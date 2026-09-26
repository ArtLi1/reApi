import logging
from contextlib import contextmanager
from threading import RLock

from HTTP.http_errors import HTTPError
from HTTP.http_module import ServerModule
from HTTP.http_parameters import ParameterBinder
from HTTP.http_request import HTTPRequest
from HTTP.http_response import HTTPResponse
from HTTP.http_router import HTTPRouter
from Utils import ContentType

logger = logging.getLogger(__name__)


class HTTPApplication:
    """可交给任意 WSGI Server 的应用；应用资源不依赖 Socket 服务器。"""

    def __init__(self):
        self._modules = {}
        self._modules_ready = False
        self._initialized_modules = []
        self._initialization_started = False
        self._state_lock = RLock()
        self.binder = ParameterBinder(self._resolve_module)
        self.router = HTTPRouter(self.binder)

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

    def error_handler(self, e: Exception) -> HTTPResponse:
        if isinstance(e, HTTPError):
            code, status, message = e.code, e.status, str(e)
        else:
            logger.error("Request failed", exc_info=(type(e), e, e.__traceback__))
            code, status, message = 500, "Internal Server Error", "Internal Server Error"
        return HTTPResponse(
            body={"error": message},
            content_type=ContentType.JSON,
            code=code,
            status=status
        )

    def startup(self):
        """显式启动且只允许一次；失败时逆序释放已尝试初始化的模块。"""
        with self._state_lock:
            if self._initialization_started:
                raise RuntimeError("Application initialization has already started")
            self._initialization_started = True
            try:
                self.router.freeze(self._modules)
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
        return self._execute(request, self.router.match(request))

    def handle_request(self, request: HTTPRequest):
        try:
            return self.dispatch_request(request)
        except Exception as error:
            return self.error_handler(error)

    def __call__(self, environ, start_response):
        # 包含请求构造和响应转换，保证非法 JSON 与 Handler 异常使用同一错误格式。
        try:
            if not self._modules_ready:
                raise RuntimeError("Use application.lifecycle() or startup() before serving")
            response = self.handle_request(HTTPRequest.from_environ(environ))
            status, headers, body = response.to_wsgi()
        except Exception as error:
            status, headers, body = self.error_handler(error).to_wsgi()
        # start_response 的服务器异常不能再次包装成另一份响应。
        start_response(status, headers)
        return [body]

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

