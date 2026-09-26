import logging
import math
import socket
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Event, Lock

from HTTP.http_protocol import HTTPProtocolError, read_http_request
from HTTP.http_wsgi import WSGIRequestHandler, build_environ

logger = logging.getLogger(__name__)


class HTTPServer:
    """运行任意 WSGI callable 的线程池服务器；每个连接处理一个请求。"""

    def __init__(self, application, host="127.0.0.1", port=8000, max_workers=8,
                 request_timeout=10.0, max_header_bytes=65536, max_body_bytes=10 * 1024 * 1024):
        if not callable(application):
            raise TypeError("application must be a WSGI callable")
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        if (isinstance(request_timeout, bool) or not isinstance(request_timeout, (int, float))
                or request_timeout <= 0 or not math.isfinite(request_timeout)):
            raise ValueError("request_timeout must be a finite number > 0")
        for name, value in (("max_header_bytes", max_header_bytes), ("max_body_bytes", max_body_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.application = application
        self.host = host
        self.port = port
        self.max_workers = max_workers
        self.request_timeout = request_timeout
        self.max_header_bytes = max_header_bytes
        self.max_body_bytes = max_body_bytes
        self._started = False
        self._state_lock = Lock()
        self._stop_event = Event()

    def stop(self):
        """停止接收连接；run 会等待正在调用、迭代和发送的 WSGI 响应结束。"""
        self._stop_event.set()

    def _handle_client(self, client, address):
        with client:
            try:
                request = read_http_request(client, self.request_timeout, self.max_header_bytes, self.max_body_bytes)
                if request is None:
                    return
            except (socket.timeout, HTTPProtocolError) as error:
                code = 408 if isinstance(error, socket.timeout) else error.code
                self._send_protocol_error(client, code)
                return
            except OSError:
                return
            environ = build_environ(request, address, self.host, self.port)
            # Server 仅调用标准接口，不能假设应用具有 router、startup 等框架方法。
            handler = WSGIRequestHandler(client, environ, multithread=self.max_workers > 1)
            handler.run(self.application)

    @staticmethod
    def _send_protocol_error(client, code):
        from http import HTTPStatus

        status = HTTPStatus(code).phrase
        body = status.encode("ascii")
        payload = (f"HTTP/1.1 {code} {status}\r\nConnection: close\r\n"
                   f"Content-Type: text/plain; charset=utf-8\r\nContent-Length: {len(body)}\r\n\r\n").encode("ascii") + body
        try:
            client.sendall(payload)
        except OSError:
            pass

    def run(self):
        with self._state_lock:
            if self._started:
                raise RuntimeError("This server instance has already been started")
            self._started = True
        slots = BoundedSemaphore(self.max_workers)

        def client_done(future):
            slots.release()
            error = future.exception()
            if error is not None:
                logger.error("Client worker failed", exc_info=(type(error), error, error.__traceback__))

        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="http") as executor:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind((self.host, self.port))
                server.listen(self.max_workers)
                server.settimeout(0.2)
                self.port = server.getsockname()[1]
                print(f"Server running: http://{self.host}:{self.port}")
                while not self._stop_event.is_set():
                    # ponytail: 不向线程池无限排队；最多 max_workers 个在途连接。
                    if not slots.acquire(timeout=0.2):
                        continue
                    try:
                        client, address = server.accept()
                    except socket.timeout:
                        slots.release()
                        continue
                    except BaseException:
                        slots.release()
                        raise
                    try:
                        if self._stop_event.is_set():
                            client.close()
                            slots.release()
                            break
                        client.settimeout(self.request_timeout)
                        future = executor.submit(self._handle_client, client, address)
                    except BaseException:
                        client.close()
                        slots.release()
                        raise
                    future.add_done_callback(client_done)
