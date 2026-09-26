"""WSGI Server 侧适配：标准 environ、字节迭代、错误恢复与响应关闭。"""
import io
import sys
from urllib.parse import unquote_to_bytes, urlsplit
from wsgiref.handlers import SimpleHandler
from wsgiref.util import is_hop_by_hop

from HTTP.http_protocol import TOKEN


def build_environ(request, address, server_name, server_port):
    url = urlsplit(request.target)
    environ = {
        "REQUEST_METHOD": request.method,
        "SCRIPT_NAME": "",
        # WSGI 用 Latin-1 字符串无损承载路径字节，Application 再按 UTF-8 解码。
        "PATH_INFO": unquote_to_bytes(url.path).decode("iso-8859-1"),
        "QUERY_STRING": url.query,
        "SERVER_NAME": server_name,
        "SERVER_PORT": str(server_port),
        "SERVER_PROTOCOL": request.version,
        "REMOTE_ADDR": address[0],
        "wsgi.input": io.BytesIO(request.body),
        "wsgi.url_scheme": "http",
    }
    for name, value in request.headers.items():
        key = name.upper().replace("-", "_")
        # 特殊字段按原始名称判断，Content_Length 不能伪装成报文长度。
        environ[key if name in ("content-type", "content-length") else "HTTP_" + key] = value
    return environ


def validate_response(status, headers):
    """在网络输出前检查类型、编码和换行，避免无效状态或响应头注入。"""
    if type(status) is not str or len(status) < 4 or not status[:3].isdigit() or status[3] != " ":
        raise ValueError("Invalid WSGI status")
    if not 100 <= int(status[:3]) <= 999 or "\r" in status or "\n" in status:
        raise ValueError("Invalid WSGI status")
    status.encode("iso-8859-1")
    lengths = []
    for name, value in headers:
        if type(name) is not str or type(value) is not str or not TOKEN.fullmatch(name):
            raise ValueError("Invalid response header")
        if any(ord(char) < 32 and char != "\t" or ord(char) == 127 for char in value):
            raise ValueError("Invalid response header value")
        value.encode("iso-8859-1")
        if is_hop_by_hop(name):
            raise ValueError("WSGI applications cannot set hop-by-hop headers")
        if name.lower() == "content-length":
            if not value.isascii() or not value.isdecimal():
                raise ValueError("Invalid response Content-Length")
            lengths.append(int(value))
    if len(lengths) > 1:
        raise ValueError("Duplicate response Content-Length")


class WSGIRequestHandler(SimpleHandler):
    http_version = "1.1"
    os_environ = {}  # 请求环境只来自当前请求，不向应用附带进程中的敏感环境变量。

    def __init__(self, client, environ, multithread=True, stderr=None):
        self.client = client
        self._body_bytes = 0
        self._used_write = False
        super().__init__(environ["wsgi.input"], None, stderr or sys.stderr, environ,
                         multithread=multithread, multiprocess=False)

    def run(self, application):
        try:
            super().run(application)
        finally:
            # write() 阶段断开也可能跳过标准库的正常收尾，始终释放迭代器。
            self.close()
            self.stdin.close()

    def start_response(self, status, headers, exc_info=None):
        if exc_info and self.headers_sent:
            try:
                raise exc_info[1].with_traceback(exc_info[2])
            finally:
                exc_info = None
        validate_response(status, headers)
        writer = super().start_response(status, headers, exc_info)

        def write(data):
            self._used_write = True
            return writer(data)

        return write

    def set_content_length(self):
        # write() 与迭代器可以混用，此时不能由迭代器长度推断完整 Body 长度。
        if not self._used_write:
            super().set_content_length()

    def cleanup_headers(self):
        super().cleanup_headers()
        self.headers["Connection"] = "close"
        if self.status[:3] in ("204", "304") or self.status.startswith("1"):
            if "Content-Length" in self.headers:
                del self.headers["Content-Length"]

    def _write(self, data):
        self.client.sendall(data)

    def _flush(self):
        pass  # sendall 已发送到 Socket，无额外的用户态输出缓冲区。

    def write(self, data):
        if type(data) is not bytes:
            raise TypeError("WSGI response chunks must be bytes")
        if not self.status:
            raise AssertionError("write() before start_response()")
        no_body = self.status[:3] in ("204", "304") or self.status.startswith("1")
        is_head = self.environ["REQUEST_METHOD"] == "HEAD"
        length = self.headers.get("Content-Length")
        if not no_body and not is_head and length is not None and self._body_bytes + len(data) > int(length):
            raise ValueError("Response exceeds Content-Length")
        self._body_bytes += len(data)
        if no_body or is_head:
            if not self.headers_sent:
                self.bytes_sent = len(data)
                self.send_headers()
            return
        super().write(data)

    def finish_response(self):
        try:
            for data in self.result:
                if type(data) is not bytes:
                    raise TypeError("WSGI response chunks must be bytes")
                if data:
                    self.write(data)
            length = self.headers.get("Content-Length") if self.headers is not None else None
            body_allowed = self.status and self.status[:3] not in ("204", "304") and not self.status.startswith("1")
            if body_allowed and self.environ["REQUEST_METHOD"] != "HEAD" and length is not None and self._body_bytes != int(length):
                raise ValueError("Response shorter than Content-Length")
            self.finish_content()
        except BaseException:
            # 保留 headers_sent，供标准库决定还能否发送 500；原响应只 close 一次。
            self._close_result()
            raise
        else:
            # close() 若失败，保留发送状态，避免完整响应后再发送一份 500。
            self._close_result()
            self.close()

    def _close_result(self):
        result, self.result = self.result, None
        close = getattr(result, "close", None)
        if close is not None:
            close()

    def close(self):
        try:
            self._close_result()
        finally:
            self.result = self.headers = self.status = self.environ = None
            self.bytes_sent = self._body_bytes = 0
            self.headers_sent = False
            self._used_write = False
