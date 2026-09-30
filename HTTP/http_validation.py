"""两个应用入口共用的 HTTP 响应校验。"""
from wsgiref.util import is_hop_by_hop

from HTTP.http_protocol import TOKEN


def validate_response(status, headers):
    """在输出前检查状态、响应头与 Content-Length。"""
    if type(status) is not str or len(status) < 4 or not status[:3].isdigit() or status[3] != " ":
        raise ValueError("Invalid response status")
    if not 100 <= int(status[:3]) <= 999 or "\r" in status or "\n" in status:
        raise ValueError("Invalid response status")
    status.encode("iso-8859-1")
    lengths = []
    for name, value in headers:
        if type(name) is not str or type(value) is not str or not TOKEN.fullmatch(name):
            raise ValueError("Invalid response header")
        if any(ord(char) < 32 and char != "\t" or ord(char) == 127 for char in value):
            raise ValueError("Invalid response header value")
        value.encode("iso-8859-1")
        if is_hop_by_hop(name):
            raise ValueError("Applications cannot set hop-by-hop headers")
        if name.lower() == "content-length":
            if not value.isascii() or not value.isdecimal():
                raise ValueError("Invalid response Content-Length")
            lengths.append(int(value))
    if len(lengths) > 1:
        raise ValueError("Duplicate response Content-Length")
