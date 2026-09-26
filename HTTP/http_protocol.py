"""Socket 入口的报文边界与限制；此层不解析 JSON，也不认识框架路由。"""
import re
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


class HTTPProtocolError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


@dataclass
class IncomingRequest:
    method: str
    target: str
    version: str
    headers: dict[str, str]
    body: bytes


def read_http_request(client, timeout, max_header_bytes, max_body_bytes):
    # 总期限防止不断发送少量数据的客户端永久占用一个工作线程。
    deadline = time.monotonic() + timeout

    def recv():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("Request deadline exceeded")
        client.settimeout(remaining)
        return client.recv(4096)

    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = recv()
        if not chunk:
            if not data:
                return None
            raise HTTPProtocolError("Incomplete request headers")
        data.extend(chunk)
        header_end = data.find(b"\r\n\r\n")
        if (header_end + 4 if header_end >= 0 else len(data)) > max_header_bytes:
            raise HTTPProtocolError("Request headers too large", 431)

    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise HTTPProtocolError("Invalid request line")
    method, target, version = parts
    if not TOKEN.fullmatch(method) or any(ord(char) <= 32 or ord(char) >= 127 for char in target):
        raise HTTPProtocolError("Invalid method or request target")
    if version not in ("HTTP/1.0", "HTTP/1.1"):
        raise HTTPProtocolError("Unsupported HTTP version", 505)
    if not target.startswith("/") or target.startswith("//") or "#" in target:
        raise HTTPProtocolError("Only origin-form request targets are supported")
    try:
        urlsplit(target)
    except ValueError as error:
        raise HTTPProtocolError("Invalid request target") from error

    headers = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if not separator or not TOKEN.fullmatch(name):
            raise HTTPProtocolError("Invalid header name")
        value = value.strip(" \t")
        if any(ord(char) < 32 and char != "\t" or ord(char) == 127 for char in value):
            raise HTTPProtocolError("Invalid header value")
        name = name.lower()
        if name in headers:
            # 长度和 Host 不合并，避免不同组件对重复字段作出不同解释。
            if name in ("content-length", "host"):
                raise HTTPProtocolError("Duplicate framing or Host header")
            headers[name] += ("; " if name == "cookie" else ", ") + value
        else:
            headers[name] = value
    if version == "HTTP/1.1" and not headers.get("host"):
        raise HTTPProtocolError("HTTP/1.1 requires Host")
    if "transfer-encoding" in headers:
        if "content-length" in headers:
            raise HTTPProtocolError("Conflicting message lengths")
        raise HTTPProtocolError("Transfer-Encoding is not supported", 501)
    length_text = headers.get("content-length", "0")
    if not re.fullmatch(r"[0-9]+", length_text):
        raise HTTPProtocolError("Invalid Content-Length")
    try:
        length = int(length_text)
    except ValueError as error:
        raise HTTPProtocolError("Invalid Content-Length") from error
    if length > max_body_bytes:
        raise HTTPProtocolError("Request body too large", 413)
    if "expect" in headers:
        if headers["expect"].lower() != "100-continue" or version != "HTTP/1.1":
            raise HTTPProtocolError("Unsupported expectation", 417)
        if len(body) < length:
            client.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
    while len(body) < length:
        chunk = recv()
        if not chunk:
            raise HTTPProtocolError("Incomplete request body")
        body.extend(chunk)
    client.settimeout(timeout)
    # 单连接单请求：只交付声明长度的内容，多读到的下一条报文不进入 Body。
    return IncomingRequest(method, target, version, headers, bytes(body[:length]))
