from HTTP import http_router


import socket

from HTTP.http_request import HTTPRequest
from HTTP.http_response import HTTPResponse
from HTTP.http_errors import HTTPError
from Utils import ContentType


class HTTPServer:
    def __init__(self, host="127.0.0.1", port=8000):
        self.host = host
        self.port = port
        self.router = http_router.HTTPRouter()

    def _recv_request(self, client: socket.socket) -> bytes:
        data = b""

        # 读取完整 Header
        while b"\r\n\r\n" not in data:
            chunk = client.recv(4096)
            if not chunk:
                return b""
            data += chunk

        head, _, body = data.partition(b"\r\n\r\n")

        # 获取 Content-Length
        content_length = next(
            (
                int(line.split(b":", 1)[1].strip())
                for line in head.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            ),
            0
        )

        # 继续读取 Body
        while len(body) < content_length:
            chunk = client.recv(4096)
            if not chunk:
                break
            body += chunk

        return head + b"\r\n\r\n" + body

    def error_handler(self, e: Exception) -> HTTPResponse:
        if isinstance(e, HTTPError):
            code, status, message = e.code, e.status, str(e)
        else:
            print(f"Request error: {e}")
            code, status, message = 500, "Internal Server Error", "Internal Server Error"
        return HTTPResponse(
            body={"error": message},
            content_type=ContentType.JSON,
            code=code,
            status=status
        )

    def run(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            server.bind((self.host, self.port))
            server.listen()

            print(f"Server running: http://{self.host}:{self.port}")

            while True:
                client, address = server.accept()
                with client:
                    try:
                        raw = self._recv_request(client)
                        if not raw:
                            continue
                        request = HTTPRequest.construct_from_bytes(raw)

                        response = self.router.router(request)
                        payload = response.response()
                    except Exception as e:
                        payload = self.error_handler(e).response()
                    try:
                        client.sendall(payload)
                    except OSError as e:
                        print(f"Response error: {e}")
