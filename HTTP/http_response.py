import json
from typing import Any

from HTTP.http_wsgi import validate_response
from Utils import ContentType


class HTTPResponse:
    def __init__(
        self,
        body: Any = "",
        content_type: ContentType = ContentType.TEXT,
        code: int = 200,
        status: str = "OK",
        headers: dict[str, str] | None = None,
        version: str = "HTTP/1.1"
    ):
        self.version = version
        self.code = code
        self.status = status
        self.content_type = content_type
        self.body = self._build_body(body)
        self.headers = headers or {}
        self._extra_headers = []
        # 自动添加 Content-Type
        self.headers["Content-Type"] = content_type.value
        # 自动添加 Content-Length
        self.headers["Content-Length"] = str(len(self.body))

    def _build_body(self, body: Any) -> bytes:
        # JSON
        if self.content_type == ContentType.JSON:
            return json.dumps(
                body,
                ensure_ascii=False
            ).encode("utf-8")
        # 普通字符串
        if isinstance(body, str):
            return body.encode("utf-8")
        # 文件等二进制
        if isinstance(body, bytes):
            return body
        # 其他类型
        return str(body).encode("utf-8")

    def add_header(self, name: str, value: str):
        """添加重复响应头，例如多个 Set-Cookie；headers 字典仍可正常修改。"""
        self._extra_headers.append((name, value))

    def to_wsgi(self):
        if type(self.body) is not bytes:
            raise TypeError("Response body must be bytes")
        if isinstance(self.code, bool) or not isinstance(self.code, int):
            raise TypeError("Response code must be an integer")
        status = f"{self.code} {self.status}"
        # after 可替换 Body；输出时重新计算长度，移除大小写不同的旧长度字段。
        headers = [(name, value) for name, value in [*self.headers.items(), *self._extra_headers]
                   if name.lower() != "content-length"]
        no_body = 100 <= self.code < 200 or self.code in (204, 304)
        body = b"" if no_body else self.body
        if self.code in (204, 304):
            # 无表示内容的状态不能附带默认 Content-Type（wsgiref.validate 会拒绝）。
            headers = [(name, value) for name, value in headers if name.lower() != "content-type"]
        if not no_body:
            headers.append(("Content-Length", str(len(body))))
        validate_response(status, headers)
        return status, headers, body

    def response(self) -> bytes:
        """兼容旧的独立序列化调用；WSGI Server 不依赖这个方法。"""
        status, items, body = self.to_wsgi()
        headers = "".join(f"{name}: {value}\r\n" for name, value in items)
        return (f"{self.version} {status}\r\n" + headers + "\r\n").encode("iso-8859-1") + body
