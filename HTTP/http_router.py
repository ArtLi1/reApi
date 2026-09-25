import re
from inspect import signature
from typing import get_args, get_origin
from urllib.parse import urlsplit, parse_qs
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

    def get_method(self, path: str):
        def decorator(func):
            self.listening['GET'][path] = func
            return func

        return decorator

    def post_method(self, path: str):
        def decorator(func):
            self.listening['POST'][path] = func
            return func

        return decorator

    def put_method(self, path: str):
        def decorator(func):
            self.listening['PUT'][path] = func
            return func

        return decorator

    def delete_method(self, path: str):
        def decorator(func):
            self.listening['DELETE'][path] = func
            return func

        return decorator

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
                    args.append(annotation(typ[0](request.path_params[name])))
                except (TypeError, ValueError) as e:
                    raise PathParameterError(f"Invalid path parameter: {name}") from e
            elif get_origin(annotation) is Query:
                if name not in request.query:
                    raise QueryParameterError(f"Missing query parameter: {name}")
                try:
                    args.append(annotation(typ[0](request.query[name])))
                except (TypeError, ValueError) as e:
                    raise QueryParameterError(f"Invalid query parameter: {name}") from e
            elif get_origin(annotation) is Body:
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
        return func(*args)

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
            for key, values in parse_qs(url.query).items()
        }
        request.path_params = {}
        routes = self.listening[method]
        # 2. 优先精确匹配
        func = routes.get(real_path)

        if func:
            return self.param_handler(request, func)
        # 3. 动态路由匹配
        for route_path, func in routes.items():

            parts = route_path.strip("/").split("/")
            regex_parts = []

            for part in parts:

                # /users/{id}
                if part.startswith("{") and part.endswith("}"):
                    name = part[1:-1]
                    regex_parts.append(f"(?P<{name}>[^/]+)")

                # /static/**
                elif part == "**":
                    regex_parts.append(".*")

                # /static/*
                elif part == "*":
                    regex_parts.append("[^/]+")

                else:
                    regex_parts.append(re.escape(part))

            pattern = "^/" + "/".join(regex_parts) + "/?$"

            match = re.fullmatch(pattern, real_path)

            if match:
                request.path_params = match.groupdict()
                return self.param_handler(request, func)
        raise RouteNotFoundError(f"No route for {method} {real_path}")
