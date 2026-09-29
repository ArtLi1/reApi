"""ASGI HTTP/lifespan 入口；同步框架逻辑在线程中运行。"""
import asyncio
import io
import sys
from threading import Event

from HTTP.http_application import HTTPApplication


class _ClientDisconnected(Exception):
    pass


class ASGIAdapter:
    """将 HTTPApplication 暴露为 ASGI 3 callable；不改变同步 Handler API。"""

    def __init__(self, application: HTTPApplication, *, max_body_bytes=10 * 1024 * 1024):
        if not isinstance(application, HTTPApplication):
            raise TypeError("application must be an HTTPApplication")
        if type(max_body_bytes) is not int or max_body_bytes < 1:
            raise ValueError("max_body_bytes must be a positive integer")
        self.application = application
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
        elif scope["type"] == "http":
            await self._http(scope, receive, send)
        else:
            raise RuntimeError("Only HTTP and lifespan ASGI scopes are supported")

    async def _lifespan(self, receive, send):
        while True:
            event = await receive()
            if event["type"] == "lifespan.startup":
                try:
                    await asyncio.to_thread(self.application.startup)
                except Exception as error:
                    await send({"type": "lifespan.startup.failed", "message": str(error)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif event["type"] == "lifespan.shutdown":
                try:
                    await asyncio.to_thread(self.application.shutdown)
                except Exception as error:
                    await send({"type": "lifespan.shutdown.failed", "message": str(error)})
                    return
                await send({"type": "lifespan.shutdown.complete"})
                return
            else:
                raise RuntimeError(f"Unexpected lifespan event: {event['type']}")

    async def _http(self, scope, receive, send):
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            if event["type"] != "http.request":
                raise RuntimeError(f"Unexpected HTTP event: {event['type']}")
            chunk = event.get("body", b"")
            if len(body) + len(chunk) > self.max_body_bytes:
                # 属于传输层限制，与原生 Server 的协议错误一样直接返回 413。
                payload = b"Request body too large"
                await send({"type": "http.response.start", "status": 413,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                        (b"content-length", str(len(payload)).encode("ascii"))]})
                await send({"type": "http.response.body",
                            "body": b"" if scope["method"] == "HEAD" else payload})
                return
            body.extend(chunk)
            if not event.get("more_body", False):
                break

        environ = self._environ(scope, bytes(body))
        loop = asyncio.get_running_loop()
        disconnected = Event()

        async def watch_disconnect():
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    disconnected.set()
                    return

        def sync_send(message):
            if disconnected.is_set():
                raise _ClientDisconnected()
            # 线程等待 send 完成，保持 ASGI 的网络背压与响应顺序。
            async def send_event():
                await send(message)

            try:
                asyncio.run_coroutine_threadsafe(send_event(), loop).result()
            except OSError as error:
                disconnected.set()
                raise _ClientDisconnected() from error

        def serve():
            result = None
            response_start = None
            started = False

            def start_response(status, headers, exc_info=None):
                nonlocal response_start
                if exc_info and started:
                    raise exc_info[1].with_traceback(exc_info[2])
                response_start = {"type": "http.response.start", "status": int(status[:3]),
                                  "headers": self._asgi_headers(headers)}
                return write

            def write(data):
                nonlocal started
                if type(data) is not bytes:
                    raise TypeError("WSGI response chunks must be bytes")
                if response_start is None:
                    raise RuntimeError("write() before start_response()")
                if not started:
                    sync_send(response_start)
                    started = True
                if data:
                    sync_send({"type": "http.response.body", "body": data, "more_body": True})

            try:
                result = self.application(environ, start_response)
                for chunk in result:
                    write(chunk)
                if not started:
                    if response_start is None:
                        raise RuntimeError("WSGI application did not call start_response()")
                    sync_send(response_start)
                sync_send({"type": "http.response.body", "body": b""})
            finally:
                try:
                    if result is not None:
                        close = getattr(result, "close", None)
                        if close is not None:
                            close()
                finally:
                    environ["wsgi.input"].close()

        watcher = asyncio.create_task(watch_disconnect())
        worker = asyncio.create_task(asyncio.to_thread(serve))
        try:
            await asyncio.shield(worker)
        except _ClientDisconnected:
            pass
        except asyncio.CancelledError:
            disconnected.set()
            try:
                await asyncio.shield(worker)
            except _ClientDisconnected:
                pass
            raise
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass

    @staticmethod
    def _asgi_headers(headers):
        return [(name.lower().encode("ascii"), value.encode("iso-8859-1"))
                for name, value in headers]

    @staticmethod
    def _environ(scope, body):
        root = scope.get("root_path", "")
        path = scope["path"]
        if root and (path == root or path.startswith(root + "/")):
            path = path[len(root):] or "/"
        server = scope.get("server") or ("localhost", 80)
        environ = {
            "REQUEST_METHOD": scope["method"],
            "SCRIPT_NAME": root.encode("utf-8").decode("iso-8859-1"),
            "PATH_INFO": path.encode("utf-8").decode("iso-8859-1"),
            "QUERY_STRING": scope.get("query_string", b"").decode("iso-8859-1"),
            "SERVER_NAME": server[0],
            "SERVER_PORT": str(server[1]) if server[1] is not None else "",
            "SERVER_PROTOCOL": "HTTP/" + scope.get("http_version", "1.1"),
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": scope.get("scheme", "http"),
            "wsgi.input": io.BytesIO(body),
            "wsgi.errors": sys.stderr,
            "wsgi.multithread": True,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
        }
        if scope.get("client"):
            environ["REMOTE_ADDR"] = scope["client"][0]
            environ["REMOTE_PORT"] = str(scope["client"][1])
        for raw_name, raw_value in scope.get("headers", ()):
            name = raw_name.decode("ascii").lower()
            if name == "content-length":
                continue  # ASGI 的分块 Body 已经重新计算长度。
            key = "CONTENT_TYPE" if name == "content-type" else "HTTP_" + name.upper().replace("-", "_")
            value = raw_value.decode("iso-8859-1")
            if key in environ:
                environ[key] += ("; " if name == "cookie" else ", ") + value
            else:
                environ[key] = value
        return environ
