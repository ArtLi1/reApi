import json
from typing import Any

from Utils import *


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

    def response(self) -> bytes:

        status_line = (
            f"{self.version} {self.code} {self.status}\r\n"
        )

        headers = "".join(
            f"{key}: {value}\r\n"
            for key, value in self.headers.items()
        )

        return (
            status_line.encode("utf-8")
            + headers.encode("utf-8")
            + b"\r\n"
            + self.body
        )