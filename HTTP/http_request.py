import json
import re
from urllib.parse import quote, unquote, urlsplit

from HTTP.http_errors import BodyParameterError
from Utils import parse_url_parameters


def _reject_json_constant(value):
    # Python 的 JSON 解码器默认接受 NaN/Infinity，JSON 请求禁止这些非标准数值。
    raise ValueError("Non-finite JSON number")


class HTTPRequest:
    def __init__(
        self,
        method: str,
        path: str,
        version: str,
        headers: dict[str, str],
        body: bytes = b""
    ):
        self.method = method
        self.path = path
        url = urlsplit(path)
        # 框架匹配已解码的路径，Path 参数不再二次解码；与标准 WSGI 入口一致。
        self.path_info = unquote(url.path)
        self.query_string = url.query
        self.script_name = ""
        self.scheme = "http"
        self.environ = None
        self.version = version
        # HTTP Header 名称不区分大小写，统一转小写方便查找
        self.headers = {
            key.lower(): value
            for key, value in headers.items()
        }
        self.raw_body = body
        self.json = None
        self.form = None
        self.files = {}
        self.query = {}
        self.path_params = {}
        self._parse_body()

    @property
    def media_type(self) -> str:
        """每次从当前 Header 读取，确保 before 修改 Header 后仍使用最新值。"""
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    def _parse_body(self):
        media_type = self.media_type

        if not self.raw_body:
            return

        # application/json
        if media_type == "application/json":
            try:
                self.json = json.loads(self.raw_body.decode("utf-8"), parse_constant=_reject_json_constant)
            except (UnicodeDecodeError, ValueError) as e:
                raise BodyParameterError("Invalid JSON body", error_type="invalid_json") from e

        # application/x-www-form-urlencoded
        elif media_type == "application/x-www-form-urlencoded":
            self.form = parse_url_parameters(self.raw_body.decode("utf-8"))

        # multipart/form-data
        elif media_type == "multipart/form-data":
            self._parse_multipart(self.headers.get("content-type", ""))

    def _parse_multipart(self, content_type: str):
        # 参数名不区分大小写；缺少 boundary 时返回明确的请求错误。
        boundary = None
        for option in content_type.split(";")[1:]:
            key, separator, value = option.partition("=")
            if separator and key.strip().lower() == "boundary":
                boundary = value.strip().strip('"')
                break
        if not boundary:
            raise BodyParameterError("Missing multipart boundary")
        separator = b"--" + boundary.encode()

        self.form = {}

        for part in self.raw_body.split(separator)[1:-1]:
            part = part.strip(b"\r\n")

            header_data, _, content = part.partition(b"\r\n\r\n")

            if not header_data:
                continue
            headers = {}
            for line in header_data.decode("utf-8").split("\r\n"):
                key, value = line.split(":", 1)
                headers[key.lower()] = value.strip()
            disposition = headers.get("content-disposition", "")
            params = {}
            for item in disposition.split(";")[1:]:
                if "=" in item:
                    key, value = item.strip().split("=", 1)
                    params[key] = value.strip('"')
            name = params.get("name")
            if not name:
                continue
            # 文件
            if "filename" in params:
                self.files[name] = {
                    "filename": params["filename"],
                    "content_type": headers.get(
                        "content-type",
                        "application/octet-stream"
                    ),
                    "content": content
                }
            # 普通表单字段
            else:
                self.form[name] = content.decode("utf-8")

    @property
    def text(self) -> str:
        return self.raw_body.decode("utf-8")

    @classmethod
    def from_environ(cls, environ):
        """仅读取声明长度，不关闭由 WSGI Server 提供的输入流。"""
        length_text = environ.get("CONTENT_LENGTH", "") or "0"
        if not re.fullmatch(r"[0-9]+", length_text):
            raise BodyParameterError("Invalid Content-Length")
        try:
            length = int(length_text)
        except ValueError as error:
            raise BodyParameterError("Invalid Content-Length") from error
        body = bytearray()
        while len(body) < length:
            chunk = environ["wsgi.input"].read(length - len(body))
            if not chunk:
                raise BodyParameterError("Incomplete request body")
            body.extend(chunk)
        headers = {
            name[5:].replace("_", "-").lower(): value
            for name, value in environ.items()
            if name.startswith("HTTP_") and name not in ("HTTP_CONTENT_TYPE", "HTTP_CONTENT_LENGTH")
        }
        for name in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            if environ.get(name) is not None:
                headers[name.replace("_", "-").lower()] = environ[name]
        # PEP 3333 的路径字符串用 Latin-1 承载原始字节；URL 路径按 UTF-8 解释。
        path_info = environ.get("PATH_INFO", "").encode("iso-8859-1").decode("utf-8", "replace")
        query = environ.get("QUERY_STRING", "")
        target = quote(path_info or "/", safe="/") + ("?" + query if query else "")
        request = cls(environ["REQUEST_METHOD"], target, environ.get("SERVER_PROTOCOL", "HTTP/1.1"), headers, bytes(body))
        request.script_name = environ.get("SCRIPT_NAME", "").encode("iso-8859-1").decode("utf-8", "replace")
        request.scheme = environ.get("wsgi.url_scheme", "http")
        request.environ = environ
        return request

    @classmethod
    def construct_from_bytes(cls, request: bytes):
        head_data, separator, body = request.partition(b"\r\n\r\n")

        if not separator:
            raise ValueError("Invalid HTTP request")

        lines = head_data.decode("iso-8859-1").split("\r\n")

        method, path, version = lines[0].split(" ", 2)

        headers = {
            key.strip(): value.strip()
            for key, value in (
                line.split(":", 1)
                for line in lines[1:]
                if ":" in line
            )
        }

        return cls(method, path, version, headers, body)

    @classmethod
    def construct_from_string(cls, request: str):
        return cls.construct_from_bytes(request.encode("utf-8"))
