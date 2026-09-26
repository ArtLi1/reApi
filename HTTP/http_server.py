from HTTP import http_router

import math
import socket
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Event, RLock

from HTTP.http_module import ServerModule
from HTTP.http_request import HTTPRequest
from HTTP.http_response import HTTPResponse
from HTTP.http_errors import HTTPError
from Utils import ContentType


class HTTPServer:
    def __init__(self, host="127.0.0.1", port=8000, max_workers=8, request_timeout=10.0):
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        if (isinstance(request_timeout, bool) or not isinstance(request_timeout, (int, float))
                or request_timeout <= 0 or not math.isfinite(request_timeout)):
            raise ValueError("request_timeout must be a finite number > 0")
        self.host = host
        self.port = port
        self.max_workers = max_workers
        self.request_timeout = request_timeout
        self._modules = {}
        self._modules_ready = False
        self._initialized_modules = []
        self._initialization_started = False
        self._started = False
        self._state_lock = RLock()
        self._stop_event = Event()
        self.router = http_router.HTTPRouter(self._resolve_module)

    def register_module(self, module_class, *args, **kwargs):
        if not isinstance(module_class, type) or not issubclass(module_class, ServerModule):
            raise TypeError("Module must inherit ServerModule")
        with self._state_lock:
            if self._started or self._initialization_started:
                raise RuntimeError("Modules must be registered before run")
            if module_class in self._modules:
                raise ValueError(f"Module already registered: {module_class.__name__}")
            module = module_class(*args, **kwargs)
            self._modules[module_class] = module
            return module

    def get_module(self, module_class):
        with self._state_lock:
            return self._modules[module_class]

    def _resolve_module(self, module_class):
        with self._state_lock:
            if not self._modules_ready:
                raise RuntimeError("Module injection is only available after server_init")
            if module_class not in self._modules:
                raise RuntimeError(f"Module is not registered: {module_class.__name__}")
            return self._modules[module_class]

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

    def server_init(self):
        """由 run 管理模块初始化，同一个服务器实例仅初始化一次。"""
        with self._state_lock:
            if self._initialization_started:
                raise RuntimeError("Server modules have already started initialization")
            self._initialization_started = True
            modules = tuple(self._modules.values())
        for module in modules:
            # 先记录，确保初始化失败时也能释放该模块已创建的部分资源。
            self._initialized_modules.append(module)
            module.server_init()
        with self._state_lock:
            # 全部模块初始化成功后才允许请求注入，避免使用尚未就绪的资源。
            self._modules_ready = True

    def _close_modules(self):
        with self._state_lock:
            self._modules_ready = False
        for module in reversed(self._initialized_modules):
            try:
                module.server_close()
            except Exception as e:
                print(f"Module close error: {e}")
        self._initialized_modules.clear()

    def stop(self):
        """停止接收新请求；run 等待正在处理的请求结束后释放模块。"""
        self._stop_event.set()

    def _handle_client(self, client: socket.socket, address):
        # 每个连接的读取、Hook、Handler、响应发送均在同一个工作线程中。
        with client:
            try:
                try:
                    raw = self._recv_request(client)
                except socket.timeout:
                    response = HTTPResponse(body="Request Timeout", code=408, status="Request Timeout")
                else:
                    if not raw:
                        return
                    request = HTTPRequest.construct_from_bytes(raw)
                    response = self.router.router(request)
                payload = response.response()
            except Exception as e:
                payload = self.error_handler(e).response()
            try:
                client.sendall(payload)
            except OSError as e:
                print(f"Response error for {address}: {e}")

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
                print(f"Client worker error: {error}")

        try:
            self.router.freeze(self._modules)
            self.server_init()
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
        finally:
            # 退出线程池会等待请求完成，再释放请求可能仍在使用的模块资源。
            self._close_modules()
