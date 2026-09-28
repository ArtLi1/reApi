"""从已注册的路由与参数声明生成明确的 OpenAPI 3.1.2 子集。"""

import json
import re
from html import escape
from http import HTTPStatus
from inspect import Parameter
from typing import get_args, get_origin

from pydantic import TypeAdapter

from HTTP.http_dependencies import Depends
from HTTP.http_models import Body, Path, Query


OPENAPI_VERSION = "3.1.2"
_BASIC_SCHEMAS = {str: {"type": "string"}, int: {"type": "integer"},
                  float: {"type": "number"}, bool: {"type": "boolean"}}


def _schema_for(adapter, components, mode):
    schema = adapter.json_schema(mode=mode)
    definitions = schema.pop("$defs", {})
    prefix = f"Schema{len(components) + 1}"
    names = {name: f"{prefix}_{index}_{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}"
             for index, name in enumerate(definitions, 1)}

    def rewrite(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key == "$ref" and isinstance(item, str) and item.startswith("#/$defs/"):
                    name = item[8:].replace("~1", "/").replace("~0", "~")
                    result[key] = f"#/components/schemas/{names[name]}" if name in names else item
                else:
                    result[key] = rewrite(item)
            return result
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return value

    for name, definition in definitions.items():
        components[names[name]] = rewrite(definition)
    return rewrite(schema)


def _callbacks(router, path, func):
    yield func, "handler"
    for stage, hooks in (("before", router._before_handlers), ("after", router._after_handlers)):
        for _, pattern, hook in hooks:
            if router._match_path(pattern, path):
                yield hook, stage


def _parameters(router, path, func, components):
    binder = router.binder
    params = {}
    body = None
    visited = set()

    def visit(callback, stage):
        nonlocal body
        if (callback, stage) in visited:
            return
        visited.add((callback, stage))
        for name, parameter in binder.parameters[(callback, stage)].items():
            if isinstance(parameter.default, Depends):
                visit(parameter.default.provider, "dependency")
                continue
            annotation, optional = binder._unwrap_optional(parameter.annotation)
            kind = get_origin(annotation)
            if kind in (Path, Query):
                location = "path" if kind is Path else "query"
                schema = dict(_BASIC_SCHEMAS[get_args(annotation)[0]])
                required = location == "path" or (not optional and parameter.default is Parameter.empty)
                if location == "query" and isinstance(parameter.default, Query):
                    schema["default"] = parameter.default.value
                key = location, name
                if key in params:
                    if params[key]["schema"]["type"] != schema["type"]:
                        raise TypeError(f"Conflicting {location} parameter types: {path} {name}")
                    params[key]["required"] |= required
                    if params[key]["required"]:
                        params[key]["schema"].pop("default", None)
                else:
                    params[key] = {"name": name, "in": location, "required": required, "schema": schema}
            elif kind is Body:
                model = get_args(annotation)[0]
                required = not optional and parameter.default is Parameter.empty
                if body is not None and body["model"] is not model:
                    raise TypeError(f"Conflicting JSON body models for route: {path}")
                if body is None:
                    body = {"model": model, "schema": _schema_for(binder._body_models[model][0],
                                                                    components, "validation"), "required": required}
                else:
                    body["required"] |= required

    for callback, stage in _callbacks(router, path, func):
        visit(callback, stage)

    template_names = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", path))
    for location, name in params:
        if location == "path" and name not in template_names:
            raise TypeError(f"Path parameter {name} is not in route: {path}")
    for name in template_names:
        params.setdefault(("path", name),
                          {"name": name, "in": "path", "required": True, "schema": {"type": "string"}})
    request_body = None if body is None else {
        "required": body["required"], "content": {"application/json": {"schema": body["schema"]}}
    }
    return list(params.values()), request_body


def _responses(declared, components):
    if not declared:
        declared = {200: None}
    result = {}
    for code, value in declared.items():
        if type(code) is not int or not 100 <= code <= 599:
            raise ValueError("Response status must be an integer from 100 to 599")
        try:
            description = HTTPStatus(code).phrase
        except ValueError:
            description = "Response"
        entry = {"description": description}
        if isinstance(value, str):
            entry["content"] = {value: {"schema": {}}}
        elif value is not None:
            entry["content"] = {"application/json": {
                "schema": _schema_for(TypeAdapter(value), components, "serialization")}}
        result[str(code)] = entry
    return result


def build_openapi(router, title, version):
    if not isinstance(title, str) or not title or not isinstance(version, str) or not version:
        raise ValueError("OpenAPI title and version must be non-empty strings")
    paths, components, shapes = {}, {}, {}
    for method, routes in router.listening.items():
        for path, func in routes.items():
            metadata = router._route_metadata[(method, path)]
            if not metadata["include_in_schema"]:
                continue
            if "*" in path:
                raise ValueError(f"Wildcard route cannot be documented; use include_in_schema=False: {path}")
            shape = router._shape(path)
            if shape in shapes and shapes[shape] != path:
                raise ValueError(f"OpenAPI paths use different names for the same template: {shapes[shape]}, {path}")
            shapes[shape] = path
            parameters, request_body = _parameters(router, path, func, components)
            operation = {"responses": _responses(metadata["responses"], components)}
            if metadata["summary"] is not None:
                operation["summary"] = metadata["summary"]
            if metadata["description"] is not None:
                operation["description"] = metadata["description"]
            if metadata["tags"]:
                operation["tags"] = list(metadata["tags"])
            if parameters:
                operation["parameters"] = parameters
            if request_body is not None:
                operation["requestBody"] = request_body
            paths.setdefault(path, {})[method.lower()] = operation
    document = {"openapi": OPENAPI_VERSION, "info": {"title": title, "version": version}, "paths": paths}
    if components:
        document["components"] = {"schemas": components}
    return document


def render_docs(document, openapi_path):
    """无外部前端依赖的简洁文档页面，数据来自同一份 OpenAPI 文档。"""
    sections = []
    for path, methods in document["paths"].items():
        for method, operation in methods.items():
            caption = escape(f"{method.upper()} {path}  {operation.get('summary', '')}")
            section = [f"<details><summary>{caption}</summary>"]
            if operation.get("description"):
                section.append(f"<p>{escape(operation['description'])}</p>")
            if operation.get("tags"):
                section.append(f"<p>标签：{escape(', '.join(operation['tags']))}</p>")
            if operation.get("parameters"):
                rows = "".join(
                    f"<tr><td>{escape(param['name'])}</td><td>{param['in']}</td>"
                    f"<td>{'是' if param['required'] else '否'}</td>"
                    f"<td>{escape(param['schema']['type'])}</td></tr>"
                    for param in operation["parameters"])
                section.append("<h3>参数</h3><table><tr><th>名称</th><th>位置</th>"
                               f"<th>必填</th><th>类型</th></tr>{rows}</table>")
            if "requestBody" in operation:
                schema = operation["requestBody"]["content"]["application/json"]["schema"]
                section.append("<h3>JSON 请求体</h3><pre>" + escape(json.dumps(schema, ensure_ascii=False, indent=2))
                               + "</pre>")
            section.append("<h3>响应</h3><ul>")
            for code, response in operation["responses"].items():
                media = ", ".join(response.get("content", {}))
                section.append(f"<li>{escape(code)} {escape(response['description'])} "
                               f"{escape(media)}")
                for media_type, content in response.get("content", {}).items():
                    if content.get("schema"):
                        schema = escape(json.dumps(content["schema"], ensure_ascii=False, indent=2))
                        section.append(f"<details><summary>{escape(code)} {escape(media_type)} 模型</summary>"
                                       f"<pre>{schema}</pre></details>")
                section.append("</li>")
            section.append("</ul></details>")
            sections.append("".join(section))
    if document.get("components", {}).get("schemas"):
        sections.append("<h2>模型</h2>")
        for name, schema in document["components"]["schemas"].items():
            detail = escape(json.dumps(schema, ensure_ascii=False, indent=2))
            sections.append(f"<details><summary>{escape(name)}</summary><pre>{detail}</pre></details>")
    title = escape(document["info"]["title"])
    return ("<!doctype html><html lang='zh'><meta charset='utf-8'><meta name='viewport' "
            "content='width=device-width, initial-scale=1'><title>" + title + " API</title>"
            "<style>body{font:16px system-ui;max-width:900px;margin:3rem auto;padding:0 1rem;"
            "color:#17212b}details{border:1px solid #ccd3dc;border-radius:8px;margin:12px 0;"
            "padding:14px}summary{cursor:pointer;font-weight:600}pre{overflow:auto;background:#f5f7fa;"
            "padding:16px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd3dc;"
            "padding:8px;text-align:left}a{color:#1769aa}</style><h1>" + title + " API</h1><p>OpenAPI "
            + escape(document["openapi"]) + " · <a href='" + escape(openapi_path, quote=True)
            + "'>查看 JSON 规范</a></p>" + "".join(sections) + "</html>")
