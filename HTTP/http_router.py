import math
import re
from functools import lru_cache
from threading import Lock
from typing import get_origin

from HTTP.http_dependencies import Depends
from HTTP.http_errors import MethodNotAllowedError, RouteNotFoundError
from HTTP.http_models import Path
from HTTP.http_parameters import ParameterBinder
from HTTP.http_protocol import TOKEN
from HTTP.http_request import HTTPRequest
from HTTP.http_response import HTTPResponse
from Utils import parse_url_parameters


class HTTPRouter:
    """保存路由和 Hook；按路径具体程度匹配，再选择 HTTP 方法。"""

    def __init__(self, binder=None, *, prefix=""):
        self.prefix = self._validate_prefix(prefix)
        self.listening = {method: {} for method in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")}
        # 同形路径共享一个资源：/{id} 与 /{name} 可用于不同方法。
        self._resources = {}
        self._ordered_resources = []
        self._before_handlers = []
        self._after_handlers = []
        self._frozen = False
        self._registration_lock = Lock()
        self.binder = binder if binder is not None else ParameterBinder()

    def freeze(self, module_classes=()):
        """应用启动后，路由和 Hook 保持只读，供各请求线程共享。"""
        with self._registration_lock:
            callbacks = [(func, "handler") for routes in self.listening.values() for func in routes.values()]
            callbacks.extend((func, "before") for _, _, func in self._before_handlers)
            callbacks.extend((func, "after") for _, _, func in self._after_handlers)
            visited = set()
            while callbacks:
                func, stage = callbacks.pop()
                if (func, stage) in visited:
                    continue
                visited.add((func, stage))
                parameters = self.binder.parameters[(func, stage)]
                for param in parameters.values():
                    if isinstance(param.default, Depends):
                        callbacks.append((param.default.provider, "dependency"))
                        continue
                    annotation, optional = self.binder._unwrap_optional(param.annotation)
                    if self.binder._is_module(annotation) and not optional and annotation not in module_classes:
                        label = getattr(func, "__name__", type(func).__name__)
                        raise TypeError(f"{label} requires unregistered module: {annotation.__name__}")
            self._frozen = True

    @staticmethod
    def _normalize_path(path):
        if not isinstance(path, str):
            raise TypeError("Route path must be a string")
        if (not path.startswith("/") or "//" in path or "?" in path or "#" in path
                or any(ord(char) < 32 or ord(char) == 127 for char in path)):
            raise ValueError("Route path must start with / and contain no empty segments, query or fragment")
        return path.rstrip("/") or "/"

    @classmethod
    def _validate_prefix(cls, prefix):
        if prefix == "":
            return ""
        prefix = cls._normalize_path(prefix)
        if any(char in prefix for char in "{}*"):
            raise ValueError("Router prefix must contain only literal path segments")
        return "" if prefix == "/" else prefix

    @staticmethod
    @lru_cache(maxsize=256)
    def _compile_path(pattern_path: str):
        pattern_path = HTTPRouter._normalize_path(pattern_path)
        if pattern_path == "/":
            return re.compile(r"^/$")
        regex_parts, names = [], set()
        for part in pattern_path[1:].split("/"):
            if re.fullmatch(r"\{[A-Za-z_][A-Za-z0-9_]*\}", part):
                name = part[1:-1]
                if name in names:
                    raise ValueError(f"Duplicate path parameter: {name}")
                names.add(name)
                regex_parts.append(f"(?P<{name}>[^/]+)")
            elif part == "**":
                regex_parts.append(".*")
            elif part == "*":
                regex_parts.append("[^/]+")
            elif any(char in part for char in "{}*"):
                raise ValueError("Path parameters and wildcards must occupy a complete segment")
            else:
                regex_parts.append(re.escape(part))
        return re.compile("^/" + "/".join(regex_parts) + "/?$")

    @staticmethod
    def _match_path(pattern_path: str, real_path: str):
        return HTTPRouter._compile_path(pattern_path).fullmatch(real_path)

    @staticmethod
    def _shape(path):
        return tuple("*" if part.startswith("{") else part for part in path.strip("/").split("/"))

    @staticmethod
    def _specificity(shape):
        # 从左到右：字面段 > 单段参数/通配符 > 多段通配符；完整路径优先。
        ranks = tuple(1 if part == "**" else 2 if part == "*" else 3 for part in shape)
        # 末尾 ** 仍是兜底，/a/**/fixed 应优先于 /a/**。
        return ranks + (0 if shape[-1] == "**" else 4,)

    def _ensure_mutable(self):
        if self._frozen:
            raise RuntimeError("Routes and hooks must be registered before application startup")

    def _prepare_route(self, path, method, func):
        pattern = self._compile_path(path)  # 注册时编译，非法声明不会留到请求阶段。
        shape = self._shape(path)
        if method in self._resources.get(shape, {}):
            raise ValueError(f"Route already registered for {method}: {path}")
        parameters = self.binder.compile(func, "handler")
        for name, param in parameters.items():
            annotation, optional = self.binder._unwrap_optional(param.annotation)
            if get_origin(annotation) is Path and not optional and name not in pattern.groupindex:
                raise TypeError(f"{func.__name__}.{name} is not in route path: {path}")
        return path, method, func, shape, pattern, parameters

    def _add_route(self, prepared):
        path, method, func, shape, pattern, parameters = prepared
        self.binder.parameters[(func, "handler")] = parameters
        self.listening.setdefault(method, {})[path] = func
        self._resources.setdefault(shape, {})[method] = (pattern, func)
        # ponytail: 注册时排序，请求仍线性扫描；路由量成为瓶颈后再引入索引树。
        self._ordered_resources = sorted(self._resources, key=self._specificity, reverse=True)

    def route(self, path: str, methods=("GET",)):
        """注册一个或多个 HTTP 方法；返回原函数，支持叠加装饰器。"""
        path = self._normalize_path(self.prefix + self._normalize_path(path))
        self._compile_path(path)
        if isinstance(methods, str):
            raise TypeError("methods must be an iterable of method names, not a string")
        methods = tuple(methods)
        if not methods or any(not isinstance(method, str) or not TOKEN.fullmatch(method) for method in methods):
            raise ValueError("methods must contain HTTP method names")
        methods = tuple(method.upper() for method in methods)
        if len(set(methods)) != len(methods):
            raise ValueError("Duplicate HTTP method in route declaration")

        def decorator(func):
            with self._registration_lock:
                self._ensure_mutable()
                # 先检查整个注册，避免多方法声明只成功一半。
                prepared = [self._prepare_route(path, method, func) for method in methods]
                for entry in prepared:
                    self._add_route(entry)
            return func

        return decorator

    def _register_hook(self, hooks, path: str, priority: int | float, stage: str):
        path = self._normalize_path(self.prefix + self._normalize_path(path))
        self._compile_path(path)
        if "{" in path or "}" in path:
            raise ValueError("Hook path only supports /* and /** wildcards")
        if (isinstance(priority, bool) or not isinstance(priority, (int, float))
                or priority < 0 or (isinstance(priority, float) and not math.isfinite(priority))):
            raise ValueError("Hook priority must be a finite number >= 0")

        def decorator(func):
            with self._registration_lock:
                self._ensure_mutable()
                self.binder.parameters[(func, stage)] = self.binder.compile(func, stage)
                hooks.append((priority, path, func))
                hooks.sort(key=lambda hook: hook[0])  # 稳定排序，相同优先级按注册先后执行。
            return func

        return decorator

    def include_router(self, router, *, prefix=""):
        """复制子路由当前的路由和 Hook；后续对子路由的注册不影响应用。"""
        if not isinstance(router, HTTPRouter):
            raise TypeError("Included router must be an HTTPRouter")
        if router is self:
            raise ValueError("A router cannot include itself")
        mount = self.prefix + self._validate_prefix(prefix)
        # 先取快照再锁父路由，避免相互挂载时产生锁顺序死锁。
        with router._registration_lock:
            routes = [(mount + path, method, func)
                      for method, paths in router.listening.items() for path, func in paths.items()]
            hooks = [(stage, priority, mount + path, func)
                     for stage, entries in (("before", router._before_handlers), ("after", router._after_handlers))
                     for priority, path, func in entries]
        with self._registration_lock:
            self._ensure_mutable()
            prepared = [self._prepare_route(self._normalize_path(path), method, func) for path, method, func in routes]
            compiled_hooks = [(stage, priority, self._normalize_path(path), func, self.binder.compile(func, stage))
                              for stage, priority, path, func in hooks]
            for _, _, path, _, _ in compiled_hooks:
                self._compile_path(path)
            for entry in prepared:
                self._add_route(entry)
            for stage, priority, path, func, parameters in compiled_hooks:
                self.binder.parameters[(func, stage)] = parameters
                entries = self._before_handlers if stage == "before" else self._after_handlers
                entries.append((priority, path, func))
                entries.sort(key=lambda hook: hook[0])

    def before_handler(self, path: str, priority: int | float):
        return self._register_hook(self._before_handlers, path, priority, "before")

    def after_handler(self, path: str, priority: int | float):
        return self._register_hook(self._after_handlers, path, priority, "after")

    def get_method(self, path: str):
        return self.route(path, ("GET",))

    def post_method(self, path: str):
        return self.route(path, ("POST",))

    def put_method(self, path: str):
        return self.route(path, ("PUT",))

    def delete_method(self, path: str):
        return self.route(path, ("DELETE",))

    def patch_method(self, path: str):
        return self.route(path, ("PATCH",))

    def head_method(self, path: str):
        return self.route(path, ("HEAD",))

    def options_method(self, path: str):
        return self.route(path, ("OPTIONS",))

    @staticmethod
    def _automatic_options(request):
        return HTTPResponse(code=204, status="No Content", headers={"Allow": ", ".join(request.allowed_methods)})

    def match(self, request: HTTPRequest):
        method, real_path = request.method.upper(), request.path_info
        request.query = parse_url_parameters(request.query_string)
        request.path_params = {}
        request.allowed_methods = ()
        for shape in self._ordered_resources:
            resource = self._resources[shape]
            pattern, _ = next(iter(resource.values()))
            if not pattern.fullmatch(real_path):
                continue
            allowed = set(resource) | {"OPTIONS"}
            if "GET" in allowed:
                allowed.add("HEAD")
            request.allowed_methods = tuple(sorted(allowed))
            selected = "GET" if method == "HEAD" and "HEAD" not in resource else method
            if selected in resource:
                pattern, func = resource[selected]
                request.path_params = pattern.fullmatch(real_path).groupdict()
                return func
            if method == "OPTIONS":
                return self._automatic_options
            # 最具体的资源不接受该方法时返回 405，不继续落入更宽泛的路径。
            raise MethodNotAllowedError(f"Method {method} is not allowed for {real_path}", request.allowed_methods)
        raise RouteNotFoundError(f"No route for {method} {real_path}")
