import json
from urllib.parse import parse_qs
from HTTP.http_errors import BodyParameterError


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

    def _parse_body(self):
        content_type = self.headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()

        if not self.raw_body:
            return

        # application/json
        if media_type == "application/json":
            try:
                self.json = json.loads(self.raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise BodyParameterError("Invalid JSON body") from e

        # application/x-www-form-urlencoded
        elif media_type == "application/x-www-form-urlencoded":
            data = parse_qs(
                self.raw_body.decode("utf-8"),
                keep_blank_values=True
            )

            self.form = {
                key: values[0] if len(values) == 1 else values
                for key, values in data.items()
            }

        # multipart/form-data
        elif media_type == "multipart/form-data":
            self._parse_multipart(content_type)

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
