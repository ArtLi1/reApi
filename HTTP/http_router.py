import math
import re
from functools import lru_cache
from inspect import signature
from threading import Lock
from types import UnionType
from typing import Union, get_args, get_origin, get_type_hints
from urllib.parse import unquote, urlsplit

from HTTP.http_errors import (
    BodyParameterError,
    PathParameterError,
    QueryParameterError,
    RouteNotFoundError,
)
from HTTP.http_models import Body, Path, Query
from HTTP.http_module import ServerModule
from HTTP.http_request import HTTPRequest
from HTTP.http_response import HTTPResponse
from Utils import parse_url_parameters


class HTTPRouter:
    def __init__(self, module_resolver=None):
        self.listening = {'GET': {}, 'POST': {}, 'PUT': {}, 'DELETE': {}}
        self._before_handlers = []
        self._after_handlers = []
        self._frozen = False
        self._registration_lock = Lock()
        self._parameters = {}
        self._module_resolver = module_resolver

    def freeze(self, module_classes=()):
        """服务器启动后，路由和 Hook 保持只读，供各请求线程共享。"""
        with self._registration_lock:
            # 模块可在函数注册后再注册，因此缺失模块在启动前统一检查。
            callbacks = [(func, "handler") for routes in self.listening.values() for func in routes.values()]
            callbacks.extend((func, "before") for _, _, func in self._before_handlers)
            callbacks.extend((func, "after") for _, _, func in self._after_handlers)
            for func, stage in callbacks:
                parameters = self._parameters[(func, stage)]
                for param in parameters.values():
                    annotation, optional = self._unwrap_optional(param.annotation)
                    if self._is_module(annotation) and not optional and annotation not in module_classes:
                        raise TypeError(f"{func.__name__} requires unregistered module: {annotation.__name__}")
            self._frozen = True

    def _register_hook(self, hooks, path: str, priority: int | float, stage: str):
        if not isinstance(path, str):
            raise TypeError("Hook path must be a string")
        if not path.startswith("/") or any(
                "{" in part or "}" in part or ("*" in part and part not in ("*", "**"))
                for part in path.strip("/").split("/")
        ):
            raise ValueError("Hook path only supports /* and /** wildcards")
        if (isinstance(priority, bool) or not isinstance(priority, (int, float))
                or priority < 0 or (isinstance(priority, float) and not math.isfinite(priority))):
            raise ValueError("Hook priority must be a finite number >= 0")

        def decorator(func):
            with self._registration_lock:
                if self._frozen:
                    raise RuntimeError("Hooks must be registered before run")
                self._parameters[(func, stage)] = self._validate_params(func, stage)
                hooks.append((priority, path, func))
                # Python 的排序稳定：相同优先级保持注册顺序。
                hooks.sort(key=lambda hook: hook[0])
            return func

        return decorator

    def _method(self, method: str, path: str):
        def decorator(func):
            with self._registration_lock:
                if self._frozen:
                    raise RuntimeError("Routes must be registered before run")
                parameters = self._validate_params(func, "handler")
                path_names = re.findall(r"\{([^{}]+)\}", path)
                for name, param in parameters.items():
                    annotation, optional = self._unwrap_optional(param.annotation)
                    if get_origin(annotation) is Path and not optional and name not in path_names:
                        raise TypeError(f"{func.__name__}.{name} is not in route path: {path}")
                self._parameters[(func, "handler")] = parameters
                self.listening[method][path] = func
            return func

        return decorator

    def before_handler(self, path: str, priority: int | float):
        return self._register_hook(self._before_handlers, path, priority, "before")

    def after_handler(self, path: str, priority: int | float):
        return self._register_hook(self._after_handlers, path, priority, "after")

    def get_method(self, path: str):
        return self._method('GET', path)

    def post_method(self, path: str):
        return self._method('POST', path)

    def put_method(self, path: str):
        return self._method('PUT', path)

    def delete_method(self, path: str):
        return self._method('DELETE', path)

    @staticmethod
    def _is_module(annotation):
        return isinstance(annotation, type) and issubclass(annotation, ServerModule)

    @staticmethod
    def _unwrap_optional(annotation):
        # 仅支持 T | None / Optional[T]，保留多类型联合供声明校验拒绝。
        if get_origin(annotation) in (Union, UnionType):
            args = get_args(annotation)
            if len(args) == 2 and type(None) in args:
                return next(arg for arg in args if arg is not type(None)), True
        return annotation, False

    def _validate_params(self, func, stage):
        hints = get_type_hints(func)
        parameters = {}
        for name, param in signature(func).parameters.items():
            if param.kind not in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
                raise TypeError(f"{func.__name__}.{name} must be a named parameter")
            declared = hints.get(name, param.annotation)
            annotation, _ = self._unwrap_optional(declared)
            kind = get_origin(annotation) or annotation
            if annotation is HTTPRequest or self._is_module(annotation):
                pass
            elif annotation is HTTPResponse:
                if stage != "after":
                    raise TypeError("HTTPResponse can only be injected into after hooks")
            elif kind in (Path, Query):
                args = get_args(annotation)
                # URL 参数只支持可明确从文本解析的四种基本类型。
                if len(args) != 1 or args[0] not in (str, int, float, bool):
                    raise TypeError(f"{func.__name__}.{name} must be Path/Query[str, int, float, or bool]")
            elif kind is Body:
                args = get_args(annotation)
                if len(args) != 1 or not isinstance(args[0], type):
                    raise TypeError(f"{func.__name__}.{name} must be Body[ModelClass]")
            else:
                raise TypeError(f"Unsupported annotation for {func.__name__}.{name}: {annotation}")
            parameters[name] = param.replace(annotation=declared)
        return parameters

    @staticmethod
    def _parse_basic(value: str, typ: type):
        if not isinstance(value, str):
            raise ValueError("Expected a single value")
        if typ is bool:
            # bool("false") 也会得到 True，因此布尔值必须显式解析。
            normalized = value.lower()
            if normalized == "true":
                return True
            if normalized == "false":
                return False
            raise ValueError("Expected true or false")
        return typ(value)

    @staticmethod
    @lru_cache(maxsize=256)
    def _compile_path(pattern_path: str):
        # 模式固定且会被各请求重复使用；有界缓存避免持续注册模式时无限增长。
        regex_parts = []
        for part in pattern_path.strip("/").split("/"):
            if part.startswith("{") and part.endswith("}"):
                regex_parts.append(f"(?P<{part[1:-1]}>[^/]+)")
            elif part == "**":
                regex_parts.append(".*")
            elif part == "*":
                regex_parts.append("[^/]+")
            else:
                regex_parts.append(re.escape(part))
        pattern = "^/" + "/".join(regex_parts) + "/?$"
        return re.compile(pattern)

    @staticmethod
    def _match_path(pattern_path: str, real_path: str):
        return HTTPRouter._compile_path(pattern_path).fullmatch(real_path)

    def param_handler(self, request: HTTPRequest, func, response=None, stage="handler"):
        """按注解绑定命名参数；模块仅从服务器获取，绝不在请求中创建。"""
        args = {}
        for name, param in self._parameters[(func, stage)].items():
            annotation, optional = self._unwrap_optional(param.annotation)
            kind = get_origin(annotation)
            if annotation is HTTPRequest:
                args[name] = request
            elif annotation is HTTPResponse:
                # after 的请求参数提前绑定；真正调用时才注入最新响应。
                if response is not None:
                    args[name] = response
            elif self._is_module(annotation):
                if self._module_resolver is None:
                    raise RuntimeError("Module injection requires an HTTPServer")
                args[name] = self._module_resolver(annotation, optional=optional)
            elif kind in (Path, Query):
                is_path = kind is Path
                values = request.path_params if is_path else request.query
                error = PathParameterError if is_path else QueryParameterError
                label = "path" if is_path else "query"
                if name not in values:
                    if optional:
                        args[name] = None
                        continue
                    if param.default is not param.empty:
                        continue
                    if is_path:
                        raise RuntimeError(f"{func.__name__}.{name} is not available in this route")
                    raise QueryParameterError(f"Missing query parameter: {name}")
                try:
                    # 匹配路由后再解码，避免 %2F 提前变成路径分隔符。
                    value = unquote(values[name]) if is_path else values[name]
                    args[name] = annotation(self._parse_basic(value, get_args(annotation)[0]))
                except (TypeError, ValueError) as e:
                    raise error(f"Invalid {label} parameter: {name}") from e
            elif kind is Body:
                # 缺失请求体可省略；显式 JSON null 或非法内容仍需报错。
                if optional and not request.raw_body and request.json is None:
                    args[name] = None
                    continue
                if not optional and not request.raw_body and param.default is not param.empty:
                    continue
                # Body[T] 只绑定 JSON 对象；其他请求体格式仍可由 HTTPRequest 单独使用。
                if request.media_type != "application/json":
                    raise BodyParameterError("Body requires application/json")
                if request.json is None:
                    raise BodyParameterError("Missing JSON body")
                if not isinstance(request.json, dict):
                    raise BodyParameterError("JSON body must be an object")
                try:
                    # 各函数分别构造模型，构造函数应只解析数据，不执行数据库等业务操作。
                    args[name] = annotation(get_args(annotation)[0](**request.json))
                except (TypeError, ValueError) as e:
                    raise BodyParameterError(f"Invalid body parameter: {name}") from e
        return args

    def request_handler(self, request: HTTPRequest, func):
        # 匹配路由后执行 before，再绑定参数；异常交给 HTTPServer 统一处理。
        real_path = urlsplit(request.path).path
        target = request.method, request.path
        for _, path, hook in self._before_handlers:
            if self._match_path(path, real_path):
                updated = hook(**self.param_handler(request, hook, stage="before"))
                if updated is not None:
                    if not isinstance(updated, HTTPRequest):
                        raise TypeError("before hooks must return HTTPRequest or None")
                    request = updated
                if (request.method, request.path) != target:
                    raise RuntimeError("before hooks cannot change the matched request method or path")
        args = self.param_handler(request, func)
        # after 若需要 Query/Path/Body，先校验，避免业务成功后才发现参数缺失。
        after_calls = [
            (hook, self.param_handler(request, hook, stage="after"))
            for _, path, hook in self._after_handlers
            if self._match_path(path, real_path)
        ]
        response = func(**args)
        if not isinstance(response, HTTPResponse):
            raise TypeError("Handlers must return HTTPResponse")
        for hook, hook_args in after_calls:
            for name, param in self._parameters[(hook, "after")].items():
                annotation, _ = self._unwrap_optional(param.annotation)
                if annotation is HTTPResponse:
                    hook_args[name] = response
            updated = hook(**hook_args)
            if updated is not None:
                if not isinstance(updated, HTTPResponse):
                    raise TypeError("after hooks must return HTTPResponse or None")
                response = updated
        return response

    def router(self, request: HTTPRequest):
        path = request.path
        method = request.method.upper()

        if method not in self.listening:
            raise RouteNotFoundError(f"No route for {method} {path}")

        # 1. 解析 URL
        url = urlsplit(path)
        real_path = url.path

        request.query = parse_url_parameters(url.query)
        request.path_params = {}
        routes = self.listening[method]
        # 2. 优先精确匹配
        func = routes.get(real_path)

        if func:
            return self.request_handler(request, func)
        # 3. 动态路由匹配
        for route_path, func in routes.items():
            match = self._match_path(route_path, real_path)

            if match:
                request.path_params = match.groupdict()
                return self.request_handler(request, func)
        raise RouteNotFoundError(f"No route for {method} {real_path}")
