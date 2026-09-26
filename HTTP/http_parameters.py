from inspect import signature
from types import UnionType
from typing import Union, get_args, get_origin, get_type_hints

from HTTP.http_errors import BodyParameterError, PathParameterError, QueryParameterError
from HTTP.http_models import Body, Path, Query
from HTTP.http_module import ServerModule
from HTTP.http_request import HTTPRequest
from HTTP.http_response import HTTPResponse


class ParameterBinder:
    """注册时解析声明，请求时按同一规则绑定 Handler 和 Hook 参数。"""

    def __init__(self, module_resolver=None):
        self.parameters = {}
        self._module_resolver = module_resolver

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

    def compile(self, func, stage):
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

    def bind(self, request: HTTPRequest, func, response=None, stage="handler"):
        """按注解绑定命名参数；模块仅从应用获取，绝不在请求中创建。"""
        args = {}
        for name, param in self.parameters[(func, stage)].items():
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
                    raise RuntimeError("Module injection requires an HTTPApplication")
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
                    # 路径和 Query 已在请求构造时解码，此处只进行类型转换。
                    value = values[name]
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

