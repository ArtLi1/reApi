import math
import re
from inspect import signature
from typing import get_args, get_origin
from urllib.parse import urlsplit, parse_qs, unquote
from HTTP.http_errors import (
    BodyParameterError,
    PathParameterError,
    QueryParameterError,
    RouteNotFoundError,
)
from HTTP.http_models import Body, Path, Query
from HTTP.http_request import HTTPRequest


class HTTPRouter:
    def __init__(self):
        self.listening = {'GET': {}, 'POST': {}, 'PUT': {}, 'DELETE': {}}
        self._before_handlers = []
        self._after_handlers = []

    @staticmethod
    def _register_hook(hooks, path: str, priority: int | float):
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
            hooks.append((priority, path, func))
            # Python 的排序稳定：相同优先级保持注册顺序。
            hooks.sort(key=lambda hook: hook[0])
            return func

        return decorator

    def _method(self, method: str, path: str):
        def decorator(func):
            self._validate_params(func)
            self.listening[method][path] = func
            return func

        return decorator

    def before_handler(self, path: str, priority: int | float):
        return self._register_hook(self._before_handlers, path, priority)

    def after_handler(self, path: str, priority: int | float):
        return self._register_hook(self._after_handlers, path, priority)

    def get_method(self, path: str):
        return self._method('GET', path)

    def post_method(self, path: str):
        return self._method('POST', path)

    def put_method(self, path: str):
        return self._method('PUT', path)

    def delete_method(self, path: str):
        return self._method('DELETE', path)

    @staticmethod
    def _validate_params(func):
        for name, param in signature(func).parameters.items():
            annotation = param.annotation
            kind = get_origin(annotation) or annotation
            if kind in (Path, Query):
                args = get_args(annotation)
                # URL 参数只支持可明确从文本解析的四种基本类型。
                if len(args) != 1 or args[0] not in (str, int, float, bool):
                    raise TypeError(f"{func.__name__}.{name} must be Path/Query[str, int, float, or bool]")

    @staticmethod
    def _parse_basic(value: str, typ: type):
        if not isinstance(value, str):
            raise ValueError("Expected a single value")
        if typ is bool:
            # bool("false") 也会得到 True，因此布尔值必须显式解析。
            if value.lower() == "true":
                return True
            if value.lower() == "false":
                return False
            raise ValueError("Expected true or false")
        return typ(value)

    @staticmethod
    def _match_path(pattern_path: str, real_path: str):
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
        return re.fullmatch(pattern, real_path)

    def param_handler(self, request: HTTPRequest, func):
        args = []
        for name, param in signature(func).parameters.items():
            annotation = param.annotation
            typ = get_args(annotation)
            if param.annotation == HTTPRequest:
                args.append(request)
            elif get_origin(annotation) is Path:
                if name not in request.path_params:
                    raise PathParameterError(f"Missing path parameter: {name}")
                try:
                    # 匹配路由后再解码，避免 %2F 提前变成路径分隔符。
                    args.append(annotation(self._parse_basic(unquote(request.path_params[name]), typ[0])))
                except (TypeError, ValueError) as e:
                    raise PathParameterError(f"Invalid path parameter: {name}") from e
            elif get_origin(annotation) is Query:
                if name not in request.query:
                    raise QueryParameterError(f"Missing query parameter: {name}")
                try:
                    args.append(annotation(self._parse_basic(request.query[name], typ[0])))
                except (TypeError, ValueError) as e:
                    raise QueryParameterError(f"Invalid query parameter: {name}") from e
            elif get_origin(annotation) is Body:
                # Body[T] 只绑定 JSON 对象；其他请求体格式仍可由 HTTPRequest 单独使用。
                media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if media_type != "application/json":
                    raise BodyParameterError("Body requires application/json")
                if request.json is None:
                    raise BodyParameterError("Missing JSON body")
                if not isinstance(request.json, dict):
                    raise BodyParameterError("JSON body must be an object")
                try:
                    args.append(annotation(typ[0](**request.json)))
                except (TypeError, ValueError) as e:
                    raise BodyParameterError(f"Invalid body parameter: {name}") from e
            else:
                args.append(None)
        return args

    def request_handler(self, request: HTTPRequest, func):
        # 参数绑定成功后才进入钩子；钩子异常交给 HTTPServer 统一处理。
        real_path = urlsplit(request.path).path
        for _, path, hook in self._before_handlers:
            if self._match_path(path, real_path):
                updated = hook(request)
                if updated is not None:
                    request = updated
        args = self.param_handler(request, func)
        response = func(*args)
        for _, path, hook in self._after_handlers:
            if self._match_path(path, real_path):
                updated = hook(request, response)
                if updated is not None:
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

        request.query = {
            key: values[0] if len(values) == 1 else values
            for key, values in parse_qs(url.query, keep_blank_values=True).items()
        }
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
