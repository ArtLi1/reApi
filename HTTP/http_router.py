import math
import re
from functools import lru_cache
from threading import Lock
from typing import get_origin

from HTTP.http_errors import RouteNotFoundError
from HTTP.http_models import Path
from HTTP.http_parameters import ParameterBinder
from HTTP.http_request import HTTPRequest
from Utils import parse_url_parameters


class HTTPRouter:
    """保存路由和 Hook 的注册信息；请求执行由 Application 组织。"""

    def __init__(self, binder=None):
        self.listening = {'GET': {}, 'POST': {}, 'PUT': {}, 'DELETE': {}}
        self._before_handlers = []
        self._after_handlers = []
        self._frozen = False
        self._registration_lock = Lock()
        self.binder = binder if binder is not None else ParameterBinder()

    def freeze(self, module_classes=()):
        """应用启动后，路由和 Hook 保持只读，供各请求线程共享。"""
        with self._registration_lock:
            # 模块可在函数注册后再注册，因此缺失模块在启动前统一检查。
            callbacks = [(func, "handler") for routes in self.listening.values() for func in routes.values()]
            callbacks.extend((func, "before") for _, _, func in self._before_handlers)
            callbacks.extend((func, "after") for _, _, func in self._after_handlers)
            for func, stage in callbacks:
                parameters = self.binder.parameters[(func, stage)]
                for param in parameters.values():
                    annotation, optional = self.binder._unwrap_optional(param.annotation)
                    if self.binder._is_module(annotation) and not optional and annotation not in module_classes:
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
                    raise RuntimeError("Hooks must be registered before application startup")
                self.binder.parameters[(func, stage)] = self.binder.compile(func, stage)
                hooks.append((priority, path, func))
                # Python 的排序稳定：相同优先级保持注册顺序。
                hooks.sort(key=lambda hook: hook[0])
            return func

        return decorator

    def _method(self, method: str, path: str):
        def decorator(func):
            with self._registration_lock:
                if self._frozen:
                    raise RuntimeError("Routes must be registered before application startup")
                parameters = self.binder.compile(func, "handler")
                path_names = re.findall(r"\{([^{}]+)\}", path)
                for name, param in parameters.items():
                    annotation, optional = self.binder._unwrap_optional(param.annotation)
                    if get_origin(annotation) is Path and not optional and name not in path_names:
                        raise TypeError(f"{func.__name__}.{name} is not in route path: {path}")
                self.binder.parameters[(func, "handler")] = parameters
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

    def match(self, request: HTTPRequest):
        path = request.path
        method = request.method.upper()

        if method not in self.listening:
            raise RouteNotFoundError(f"No route for {method} {path}")

        # 1. 解析 URL
        real_path = request.path_info

        request.query = parse_url_parameters(request.query_string)
        request.path_params = {}
        routes = self.listening[method]
        # 2. 优先精确匹配
        func = routes.get(real_path)

        if func:
            return func
        # 3. 动态路由匹配
        for route_path, func in routes.items():
            match = self._match_path(route_path, real_path)

            if match:
                request.path_params = match.groupdict()
                return func
        raise RouteNotFoundError(f"No route for {method} {real_path}")
