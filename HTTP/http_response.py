import json
import mimetypes
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel

from HTTP.http_wsgi import validate_response
from Utils import ContentType


class HTTPResponse:
    def __init__(
        self,
        body: Any = "",
        content_type: ContentType | str = ContentType.TEXT,
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
        self.headers["Content-Type"] = content_type.value if isinstance(content_type, ContentType) else content_type
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

    def set_cookie(self, name: str, value: str, *, path="/", domain=None, max_age=None,
                   expires=None, secure=False, httponly=False, samesite=None):
        """逐条追加 Set-Cookie，保留同一响应中的其他 Cookie。"""
        cookie = SimpleCookie()
        cookie[name] = value
        if name not in cookie:
            raise ValueError("Invalid cookie name")
        morsel = cookie[name]
        for key, option in (("path", path), ("domain", domain), ("max-age", max_age),
                            ("expires", expires), ("samesite", samesite)):
            if option is not None:
                morsel[key] = option
        if secure:
            morsel["secure"] = True
        if httponly:
            morsel["httponly"] = True
        self.add_header("Set-Cookie", morsel.OutputString())

    def delete_cookie(self, name: str, *, path="/", domain=None):
        # 删除时 Path/Domain 应与设置时一致，浏览器才能定位同一个 Cookie。
        self.set_cookie(name, "", path=path, domain=domain, max_age=0,
                        expires="Thu, 01 Jan 1970 00:00:00 GMT")

    def _body_for_wsgi(self):
        if type(self.body) is not bytes:
            raise TypeError("Response body must be bytes")
        return self.body, len(self.body)

    def to_wsgi(self):
        body, content_length = self._body_for_wsgi()
        if isinstance(self.code, bool) or not isinstance(self.code, int):
            raise TypeError("Response code must be an integer")
        status = f"{self.code} {self.status}"
        # after 可替换 Body；输出时重新计算长度，移除大小写不同的旧长度字段。
        headers = [(name, value) for name, value in [*self.headers.items(), *self._extra_headers]
                   if name.lower() != "content-length"]
        no_body = 100 <= self.code < 200 or self.code in (204, 304)
        if no_body:
            body, content_length = b"", None
        if self.code in (204, 304):
            # 无表示内容的状态不能附带默认 Content-Type（wsgiref.validate 会拒绝）。
            headers = [(name, value) for name, value in headers if name.lower() != "content-type"]
        if content_length is not None:
            headers.append(("Content-Length", str(content_length)))
        validate_response(status, headers)
        return status, headers, body

    def response(self) -> bytes:
        """兼容旧的独立序列化调用；WSGI Server 不依赖这个方法。"""
        status, items, body = self.to_wsgi()
        headers = "".join(f"{name}: {value}\r\n" for name, value in items)
        return (f"{self.version} {status}\r\n" + headers + "\r\n").encode("iso-8859-1") + body


class JSONResponse(HTTPResponse):
    def __init__(self, body: Any, **kwargs):
        if isinstance(body, BaseModel):
            body = body.model_dump(mode="json")
        super().__init__(body, content_type=ContentType.JSON, **kwargs)


class StreamingResponse(HTTPResponse):
    def __init__(self, body, *, content_type: ContentType | str = ContentType.BINARY,
                 content_length: int | None = None, **kwargs):
        if isinstance(body, (str, bytes)) or not hasattr(body, "__iter__"):
            raise TypeError("Streaming body must be an iterable of bytes")
        if content_length is not None and (type(content_length) is not int or content_length < 0):
            raise ValueError("content_length must be a non-negative integer")
        super().__init__(b"", content_type=content_type, **kwargs)
        self._stream = body
        self.content_length = content_length
        self.headers.pop("Content-Length")

    def _body_for_wsgi(self):
        # 多次校验响应时只返回源迭代器，不在这里读取或打开资源。
        return self._stream, self.content_length

    def response(self):
        raise TypeError("StreamingResponse must be consumed through WSGI")


class FileResponse(StreamingResponse):
    def __init__(self, path, *, filename=None, chunk_size=64 * 1024, content_type=None, **kwargs):
        if type(chunk_size) is not int or chunk_size < 1:
            raise ValueError("chunk_size must be a positive integer")
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(file_path)

        def chunks():
            with file_path.open("rb") as source:
                while part := source.read(chunk_size):
                    yield part

        media_type = content_type or mimetypes.guess_type(file_path.name)[0] or ContentType.BINARY.value
        super().__init__(chunks(), content_type=media_type, content_length=file_path.stat().st_size, **kwargs)
        if filename is not None:
            name = Path(filename).name
            self.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name, safe='')}"
