import logging
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
from inspect import iscoroutinefunction, signature
from threading import RLock
from time import monotonic

from HTTP.http_dependencies import DependencyScope
from HTTP.http_errors import HTTPError, ParameterValidationError
from HTTP.http_module import ServerModule
from HTTP.http_openapi import build_openapi, render_docs
from HTTP.http_parameters import ParameterBinder
from HTTP.http_request import HTTPRequest
from HTTP.http_response import HTTPResponse, JSONResponse, StreamingResponse
from HTTP.http_router import HTTPRouter
from Utils import ContentType

logger = logging.getLogger(__name__)


class _ResponseBody:
    """共用响应迭代器；结束、出错和提前关闭时只释放一次。"""

    def __init__(self, body, on_finish, discard_source=None):
        self._body = body
        self._iterator = None
        self._discard_source = discard_source
        self._on_finish = on_finish

    def __iter__(self):
        return self

    def __next__(self):
        if self._on_finish is None:
            raise StopIteration
        try:
            if self._iterator is None:
                self._iterator = iter((self._body,)) if type(self._body) is bytes else iter(self._body)
            chunk = next(self._iterator)
            if type(chunk) is not bytes:
                raise TypeError("Streaming response chunks must be bytes")
            return chunk
        except StopIteration:
            self._finish("completed")
            raise
        except BaseException:
            self._finish("failed")
            raise

    def close(self):
        self._finish("closed")

    def _finish(self, outcome):
        callback, self._on_finish = self._on_finish, None
        source, iterator, discard = self._body, self._iterator, self._discard_source
        self._body = self._iterator = self._discard_source = None
        try:
            # HEAD/无正文状态也必须关闭被忽略的流。
            for target in (discard, source if discard is None else None, iterator if iterator is not source else None):
                close = getattr(target, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:
                        logger.exception("Response body close failed")
        finally:
            if callback is not None:
                callback(outcome)


@dataclass(frozen=True, slots=True)
class _PreparedResponse:
    code: int
    reason: str
    headers: list[tuple[str, str]]
    body: _ResponseBody


class HTTPApplication:
    """共用 HTTP 处理逻辑；WSGI 直接调用，ASGI 通过适配器调用。"""

    def __init__(self):
        self._modules = {}
        self._modules_ready = False
        self._initialized_modules = []
        self._initialization_started = False
        self._exception_handlers = {}
        self._middlewares = []
        self._state_lock = RLock()
        self._docs_config = None
        self._openapi_document = None
        self._docs_html = None
        self.binder = ParameterBinder(self._resolve_module)
        self.router = HTTPRouter(self.binder)

    def openapi(self, *, title="httpDemo", version="0.1.0"):
        """返回独立的规范副本；启用文档后使用启动时生成的版本。"""
        if self._openapi_document is not None:
            return deepcopy(self._openapi_document)
        if self._docs_config is not None:
            title, version, _ = self._docs_config
        return build_openapi(self.router, title, version)

    def enable_docs(self, *, title="httpDemo", version="0.1.0",
                    docs_path="/docs", openapi_path="/openapi.json"):
        """注册文档端点，启动时一次性生成规范。"""
        if self._docs_config is not None:
            raise RuntimeError("Documentation is already enabled")
        docs_path = self.router._normalize_path(docs_path)
        openapi_path = self.router._normalize_path(openapi_path)
        if docs_path == openapi_path:
            raise ValueError("Documentation paths must differ")
        if not isinstance(title, str) or not title or not isinstance(version, str) or not version:
            raise ValueError("Documentation title and version must be non-empty strings")
        for path in (docs_path, openapi_path):
            if "GET" in self.router._resources.get(self.router._shape(path), {}):
                raise ValueError(f"Documentation route already registered: {path}")

        @self.router.get_method(openapi_path, include_in_schema=False)
        def openapi_json():
            return JSONResponse(self._openapi_document)

        @self.router.get_method(docs_path, include_in_schema=False)
        def documentation():
            return HTTPResponse(self._docs_html, content_type=ContentType.HTML)

        self._docs_config = title, version, openapi_path

    def register_module(self, module_class, *args, **kwargs):
        if not isinstance(module_class, type) or not issubclass(module_class, ServerModule):
            raise TypeError("Module must inherit ServerModule")
        with self._state_lock:
            if self._initialization_started:
                raise RuntimeError("Modules must be registered before application startup")
            if module_class in self._modules:
                raise ValueError(f"Module already registered: {module_class.__name__}")
            module = module_class(*args, **kwargs)
            self._modules[module_class] = module
            return module

    def get_module(self, module_class):
        with self._state_lock:
            return self._modules[module_class]

    def _resolve_module(self, module_class, optional=False):
        with self._state_lock:
            if not self._modules_ready:
                raise RuntimeError("Module injection is only available after application startup")
            if module_class not in self._modules:
                if optional:
                    return None
                raise RuntimeError(f"Module is not registered: {module_class.__name__}")
            return self._modules[module_class]

    def add_exception_handler(self, exception_class, handler):
        """注册同步回调 handler(request, error)；请求构造失败时 request 为 None。"""
        if not isinstance(exception_class, type) or not issubclass(exception_class, Exception):
            raise TypeError("Exception handler key must be an Exception subclass")
        if (not callable(handler) or iscoroutinefunction(handler)
                or iscoroutinefunction(getattr(handler, "__call__", None))):
            raise TypeError("Exception handler must be a synchronous callable")
        signature(handler).bind(None, Exception())
        with self._state_lock:
            if self._initialization_started:
                raise RuntimeError("Exception handlers must be registered before application startup")
            if exception_class in self._exception_handlers:
                raise ValueError(f"Exception handler already registered: {exception_class.__name__}")
            self._exception_handlers[exception_class] = handler

    def exception_handler(self, exception_class):
        def decorator(handler):
            self.add_exception_handler(exception_class, handler)
            return handler
        return decorator

    def add_middleware(self, middleware):
        """注册同步 middleware(request, call_next)；第一个注册的最外层执行。"""
        if (not callable(middleware) or iscoroutinefunction(middleware)
                or iscoroutinefunction(getattr(middleware, "__call__", None))):
            raise TypeError("Middleware must be a synchronous callable")
        signature(middleware).bind(None, lambda: None)
        with self._state_lock:
            if self._initialization_started:
                raise RuntimeError("Middleware must be registered before application startup")
            self._middlewares.append(middleware)
        return middleware

    def middleware(self, middleware):
        """允许直接使用 @app.middleware 注册。"""
        return self.add_middleware(middleware)

    def error_handler(self, e: Exception, request=None) -> HTTPResponse:
        if not isinstance(e, HTTPError):
            logger.error("Request failed", exc_info=(type(e), e, e.__traceback__))
        # 按 MRO 选择最具体的已注册类型，注册先后不影响优先级。
        handler = next((self._exception_handlers[cls] for cls in type(e).__mro__
                        if cls in self._exception_handlers), None)
        if handler is not None:
            try:
                response = handler(request, e)
                if not isinstance(response, HTTPResponse):
                    raise TypeError("Exception handlers must return HTTPResponse")
                # 在返回前检查响应，避免错误处理器的错误再次进入同一个处理器。
                response.to_http()
                return response
            except Exception:
                logger.exception("Exception handler failed")
                e = RuntimeError("Exception handler failed")
        if isinstance(e, HTTPError):
            code, status, message = e.code, e.status, str(e)
            error_code = e.error_code
        else:
            code, status, message = 500, "Internal Server Error", "Internal Server Error"
            error_code = "internal_server_error"
        # error 保留旧接口兼容；code/message/errors 是新的稳定错误结构。
        payload = {"code": error_code, "message": message, "error": message}
        if isinstance(e, ParameterValidationError):
            payload["errors"] = e.errors
        return HTTPResponse(
            body=payload,
            content_type=ContentType.JSON,
            code=code,
            status=status,
            headers=dict(getattr(e, "headers", {})) if isinstance(e, HTTPError) else None
        )

    def startup(self):
        """显式启动且只允许一次；失败时逆序释放已尝试初始化的模块。"""
        with self._state_lock:
            if self._initialization_started:
                raise RuntimeError("Application initialization has already started")
            self._initialization_started = True
            try:
                self.router.freeze(self._modules)
                if self._docs_config is not None:
                    title, version, openapi_path = self._docs_config
                    self._openapi_document = build_openapi(self.router, title, version)
                    self._docs_html = render_docs(self._openapi_document, openapi_path)
                for module in self._modules.values():
                    self._initialized_modules.append(module)
                    module.server_init()
                self._modules_ready = True
            except BaseException:
                self.shutdown()
                raise

    def shutdown(self):
        """调用者先停止并等待请求完成；重复关闭不会重复释放模块。"""
        with self._state_lock:
            self._modules_ready = False
            for module in reversed(self._initialized_modules):
                try:
                    module.server_close()
                except Exception:
                    logger.exception("Module close failed")
            self._initialized_modules.clear()

    @contextmanager
    def lifecycle(self):
        # WSGI 没有 startup/shutdown 协议，启动入口显式管理每个进程的资源。
        self.startup()
        try:
            yield self
        finally:
            self.shutdown()

    def dispatch_request(self, request: HTTPRequest):
        """执行内部流程并保留异常，供测试或上层异常边界调用。"""
        owns_scope = request._dependency_scope is None or request._dependency_scope.closed
        if owns_scope:
            request._dependency_scope = DependencyScope(self.binder)
        try:
            func = self.router.match(request)
            # 自动 OPTIONS 仅描述路由能力，不执行业务参数绑定及路由 Hook。
            if func is self.router._automatic_options:
                return func(request)
            response = self._execute(request, func)
            if owns_scope and isinstance(response, StreamingResponse):
                raise RuntimeError("StreamingResponse must be consumed through WSGI or ASGI")
            return response
        except BaseException as error:
            if owns_scope:
                request._dependency_scope.close(error)
            raise
        finally:
            if owns_scope:
                request._dependency_scope.close()

    def handle_request(self, request: HTTPRequest, *, _defer_dependencies=False):
        request._dependency_scope = DependencyScope(self.binder)

        def run(index):
            try:
                if index == len(self._middlewares):
                    # WSGI 请求延迟解析 Body，非法 JSON 也会经过 Middleware。
                    if not request._body_parsed:
                        request._parse_body()
                        request._body_parsed = True
                    response = self.dispatch_request(request)
                else:
                    called = False
                    active = True

                    def call_next():
                        nonlocal called
                        if not active:
                            raise RuntimeError("call_next() is only valid while Middleware is running")
                        if called:
                            raise RuntimeError("call_next() may only be called once")
                        called = True
                        return run(index + 1)

                    try:
                        response = self._middlewares[index](request, call_next)
                    finally:
                        active = False
                if not isinstance(response, HTTPResponse):
                    raise TypeError("Middleware must return HTTPResponse")
                # 在每一层检查输出，外层 Middleware 才能看见无效响应转换成的 500。
                response.to_http()
                return response
            except Exception as error:
                # 错误响应生成前先释放已获取的资源。
                request._dependency_scope.close(error)
                # 每一层都把下游错误转为 Response，外层 Middleware 可统一处理 4xx/5xx。
                return self.error_handler(error, request)

        try:
            response = run(0)
            if not _defer_dependencies and isinstance(response, StreamingResponse):
                raise RuntimeError("StreamingResponse must be consumed through WSGI or ASGI")
            return response
        except BaseException as error:
            request._dependency_scope.close(error)
            raise
        finally:
            if not _defer_dependencies:
                request._dependency_scope.close()

    def _prepare_response(self, request_factory, *, method, path, started):
        """共用调度、错误转换和请求级资源清理。"""
        request = None
        try:
            if not self._modules_ready:
                raise RuntimeError("Use application.lifecycle() or startup() before serving")
            request = request_factory()
            response = self.handle_request(request, _defer_dependencies=True)
            code, headers, body = response.to_http()
        except Exception as error:
            if request is not None and request._dependency_scope is not None:
                request._dependency_scope.close(error)
            response = self.error_handler(error, request)
            code, headers, body = response.to_http()
        request_id = getattr(request.state, "request_id", None) if request is not None else None

        def finished(outcome):
            if request is not None and request._dependency_scope is not None:
                request._dependency_scope.close()
            logger.info("HTTP %r %r %s %.2fms request_id=%r outcome=%s",
                        method, path, code, (monotonic() - started) * 1000,
                        request_id, outcome)

        # 两种入口使用相同的 HEAD 与无正文状态语义。
        # Content-Length 保留正常响应长度，after 仍处理完整 Response。
        suppress_body = method.upper() == "HEAD" or 100 <= code < 200 or code in (204, 304)
        discarded = response._stream if suppress_body and isinstance(response, StreamingResponse) else None
        return _PreparedResponse(code, response.status, headers,
                                 _ResponseBody(b"" if suppress_body else body, finished, discarded))

    def __call__(self, environ, start_response):
        """WSGI 入口：只处理 environ 和 start_response 的协议转换。"""
        prepared = self._prepare_response(
            lambda: HTTPRequest.from_environ(environ, parse_body=False),
            method=environ.get("REQUEST_METHOD", ""), path=environ.get("PATH_INFO", ""),
            started=monotonic())
        try:
            start_response(f"{prepared.code} {prepared.reason}", prepared.headers)
        except BaseException:
            prepared.body._finish("start_failed")
            raise
        return prepared.body

    def _execute(self, request: HTTPRequest, func):
        # 匹配路由后执行 before，再绑定参数；异常交给 Application 统一处理。
        real_path = request.path_info
        target = request.method, request.path, request.path_info, request.query_string
        for _, path, hook in self.router._before_handlers:
            if self.router._match_path(path, real_path):
                updated = hook(**self.binder.bind(request, hook, stage="before"))
                if updated is not None:
                    if not isinstance(updated, HTTPRequest):
                        raise TypeError("before hooks must return HTTPRequest or None")
                    updated.state = request.state
                    updated._dependency_scope = request._dependency_scope
                    request = updated
                if (request.method, request.path, request.path_info, request.query_string) != target:
                    raise RuntimeError("before hooks cannot change the matched request method or path")
        args = self.binder.bind(request, func)
        # after 若需要 Query/Path/Body，先校验，避免业务成功后才发现参数缺失。
        after_calls = [
            (hook, self.binder.bind(request, hook, stage="after"))
            for _, path, hook in self.router._after_handlers
            if self.router._match_path(path, real_path)
        ]
        response = func(**args)
        if not isinstance(response, HTTPResponse):
            raise TypeError("Handlers must return HTTPResponse")
        for hook, hook_args in after_calls:
            for name, param in self.binder.parameters[(hook, "after")].items():
                annotation, _ = self.binder._unwrap_optional(param.annotation)
                if annotation is HTTPResponse:
                    hook_args[name] = response
            updated = hook(**hook_args)
            if updated is not None:
                if not isinstance(updated, HTTPResponse):
                    raise TypeError("after hooks must return HTTPResponse or None")
                response = updated
        return response

