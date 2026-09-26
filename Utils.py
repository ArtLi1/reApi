from enum import Enum
from urllib.parse import parse_qs


def parse_url_parameters(value: str) -> dict[str, str | list[str]]:
    """单个值返回字符串，重复键保留列表，空值保留为空字符串。"""
    return {
        key: values[0] if len(values) == 1 else values
        for key, values in parse_qs(value, keep_blank_values=True).items()
    }


def dictToHeaders(headers: dict) -> str:
    return "".join(f"{k}:{v}\r\n" for k, v in headers.items())


class ContentType(Enum):
    TEXT = "text/plain; charset=utf-8"
    HTML = "text/html; charset=utf-8"
    JSON = "application/json; charset=utf-8"
    XML = "application/xml; charset=utf-8"

    PNG = "image/png"
    JPEG = "image/jpeg"
    GIF = "image/gif"

    PDF = "application/pdf"
    ZIP = "application/zip"
    BINARY = "application/octet-stream"

    FORM = "application/x-www-form-urlencoded"
    MULTIPART = "multipart/form-data"


def return_type_headers(content_type: ContentType) -> dict[str, str]:
    return {"Content-Type": content_type.value}

