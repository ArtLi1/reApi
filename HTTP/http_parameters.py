import json
from dataclasses import is_dataclass
from inspect import isfunction, ismethod, signature
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, create_model

from HTTP.http_dependencies import Depends
from HTTP.http_errors import BodyParameterError, PathParameterError, QueryParameterError
from HTTP.http_models import Body, Path, Query
from HTTP.http_module import ServerModule
from HTTP.http_request import HTTPRequest
from HTTP.http_response import HTTPResponse


class ParameterBinder:
    """注册时解析声明，请求时按同一规则绑定 Handler 和 Hook 参数。"""

    def __init__(self, module_resolver=None):
        self.parameters = {}
        self._body_models = {}
        self._module_resolver = module_resolver
        self._compiling = set()

    def _compile_body_model(self, model):
        if model in self._body_models:
            return
        if issubclass(model, BaseModel) or is_dataclass(model):
            adapter, constructor = TypeAdapter(model), None
        else:
            # 兼容普通构造类：根据构造参数生成校验模型；未注解参数保留 Any。
            hints = get_type_hints(model.__init__, include_extras=True)
            class_hints = get_type_hints(model, include_extras=True)
            fields = {}
            parameters = {} if model.__init__ is object.__init__ else signature(model).parameters
            for name, param in parameters.items():
                if param.kind not in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
                    raise TypeError(f"Body model {model.__name__}.{name} must be a named parameter")
                if name.startswith("_"):
                    raise TypeError(f"Body model {model.__name__}.{name} must be a public field")
                fields[name] = (hints.get(name, class_hints.get(name, Any)),
                                ... if param.default is param.empty else param.default)
            schema = create_model(f"{model.__name__}Body", __config__=ConfigDict(validate_default=True), **fields)
            adapter, constructor = TypeAdapter(schema), model
        # 注册时构建并缓存，Handler / Hook 请求阶段只执行校验和对象构造。
        adapter.rebuild(raise_errors=True)
        self._body_models[model] = adapter, constructor

    def _parse_body_model(self, model, data):
        adapter, constructor = self._body_models[model]
        # 使用 JSON 校验入口，严格模式仍可从 JSON 对象构造嵌套 dataclass。
        # 使用当前 json，保证 before 对数据的修改会参与校验。
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
        value = adapter.validate_json(encoded, strict=True, extra="forbid")
        if constructor is None:
            return value
        return constructor(**{name: getattr(value, name) for name in type(value).model_fields})

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
        key = func, stage
        if key in self._compiling:
            raise TypeError(f"Circular dependency involving {getattr(func, '__name__', func)}")
        self._compiling.add(key)
        try:
            hint_source = func if isfunction(func) or ismethod(func) else (
                func.__init__ if isinstance(func, type) else getattr(func, "func", getattr(func, "__call__", func)))
            hints = get_type_hints(hint_source)
            parameters = {}
            for name, param in signature(func).parameters.items():
                label = getattr(func, "__name__", type(func).__name__)
                if param.kind not in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
                    raise TypeError(f"{label}.{name} must be a named parameter")
                if isinstance(param.default, Depends):
                    provider = param.default.provider
                    if (provider, "dependency") not in self.parameters:
                        self.parameters[(provider, "dependency")] = self.compile(provider, "dependency")
                    parameters[name] = param
                    continue
                declared = hints.get(name, param.annotation)
                annotation, optional = self._unwrap_optional(declared)
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
                        raise TypeError(f"{label}.{name} must be Path/Query[str, int, float, or bool]")
                elif kind is Body:
                    args = get_args(annotation)
                    if len(args) != 1 or not isinstance(args[0], type):
                        raise TypeError(f"{label}.{name} must be Body[ModelClass]")
                    self._compile_body_model(args[0])
                else:
                    raise TypeError(f"Unsupported annotation for {label}.{name}: {annotation}")
                if not optional and param.default is not param.empty and kind in (Path, Query, Body):
                    if not isinstance(param.default, kind):
                        raise TypeError(f"{label}.{name} default must be a {kind.__name__} value")
                    typ = get_args(annotation)[0]
                    valid = isinstance(param.default.value, typ) if kind is Body else type(param.default.value) is typ
                    if not valid:
                        raise TypeError(f"{label}.{name} default does not match its annotation")
                parameters[name] = param.replace(annotation=declared)
            return parameters
        finally:
            self._compiling.remove(key)

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
            if isinstance(param.default, Depends):
                args[name] = request._dependency_scope.resolve(param.default, request)
                continue
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
                        label = getattr(func, "__name__", type(func).__name__)
                        raise RuntimeError(f"{label}.{name} is not available in this route")
                    raise QueryParameterError(f"Missing query parameter: {name}",
                                              location=(label, name), error_type="missing")
                try:
                    # 路径和 Query 已在请求构造时解码，此处只进行类型转换。
                    value = values[name]
                    args[name] = annotation(self._parse_basic(value, get_args(annotation)[0]))
                except (TypeError, ValueError) as e:
                    raise error(f"Invalid {label} parameter: {name}",
                                location=(label, name), error_type="invalid_type") from e
            elif kind is Body:
                # 缺失请求体可省略；显式 JSON null 或非法内容仍需报错。
                missing = not request.raw_body and request.json is None
                if optional and missing:
                    args[name] = None
                    continue
                if missing and param.default is not param.empty:
                    continue
                if missing:
                    raise BodyParameterError("Missing JSON body", error_type="missing")
                # Body[T] 只绑定 JSON 对象；其他请求体格式仍可由 HTTPRequest 单独使用。
                if request.media_type != "application/json":
                    raise BodyParameterError("Body requires application/json", error_type="invalid_media_type")
                if not isinstance(request.json, dict):
                    raise BodyParameterError("JSON body must be an object", error_type="invalid_type")
                try:
                    # 各函数分别构造模型，构造函数应只解析数据，不执行数据库等业务操作。
                    args[name] = annotation(self._parse_body_model(get_args(annotation)[0], request.json))
                except ValidationError as e:
                    errors = [{"location": ["body", *item["loc"]], "message": item["msg"], "type": item["type"]}
                              for item in e.errors(include_input=False, include_context=False, include_url=False)]
                    raise BodyParameterError("JSON body validation failed", errors=errors) from e
                except (TypeError, ValueError) as e:
                    raise BodyParameterError(f"Invalid body parameter: {name}", error_type="invalid_value") from e
        return args

