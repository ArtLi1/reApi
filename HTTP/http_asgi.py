"""ASGI HTTP/lifespan 入口；同步框架逻辑在线程中运行。"""
import asyncio
from threading import Event
from time import monotonic

from HTTP.http_application import HTTPApplication
from HTTP.http_request import HTTPRequest


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
        chunks = []
        body_size = 0
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            if event["type"] != "http.request":
                raise RuntimeError(f"Unexpected HTTP event: {event['type']}")
            chunk = event.get("body", b"")
            if type(chunk) is not bytes:
                raise TypeError("ASGI request body must be bytes")
            if body_size + len(chunk) > self.max_body_bytes:
                # 属于传输层限制，与原生 Server 的协议错误一样直接返回 413。
                payload = b"Request body too large"
                await send({"type": "http.response.start", "status": 413,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                        (b"content-length", str(len(payload)).encode("ascii"))]})
                await send({"type": "http.response.body",
                            "body": b"" if scope["method"] == "HEAD" else payload})
                return
            body_size += len(chunk)
            if chunk:
                chunks.append(chunk)
            if not event.get("more_body", False):
                break

        # 单块直接复用；多块只在接收完毕后合并一次。
        body = b"".join(chunks)
        del chunks
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
            prepared = None
            try:
                prepared = self.application._prepare_response(
                    lambda: HTTPRequest.from_asgi_scope(scope, body, parse_body=False),
                    method=scope["method"], path=scope["path"], started=monotonic())
                sync_send({"type": "http.response.start", "status": prepared.code,
                           "headers": self._asgi_headers(prepared.headers)})
                for chunk in prepared.body:
                    if chunk:
                        sync_send({"type": "http.response.body", "body": chunk, "more_body": True})
                sync_send({"type": "http.response.body", "body": b""})
            finally:
                if prepared is not None:
                    prepared.body.close()

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
