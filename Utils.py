from enum import Enum


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
    return {
        "Content-Type": content_type.value
    }

