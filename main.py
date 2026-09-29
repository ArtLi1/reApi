"""运行示例：python main.py，或使用 --server wsgiref/asgi。"""
import argparse
import logging
from wsgiref.simple_server import make_server

from HTTP import ASGIAdapter, HTTPServer
from examples.basic import create_application


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", choices=("native", "wsgiref", "asgi"), default="native")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    app = create_application()
    if args.server == "asgi":
        try:
            import uvicorn
        except ImportError as error:
            raise RuntimeError("Install uvicorn to run the ASGI server") from error
        # ASGI Server 通过 lifespan 管理模块，避免与 WSGI 的显式启动重复。
        uvicorn.run(ASGIAdapter(app), host=args.host, port=args.port, lifespan="on")
        return
    # 停止并等待服务器请求结束后，才退出上下文并释放应用资源。
    with app.lifecycle():
        if args.server == "native":
            HTTPServer(app, host=args.host, port=args.port).run()
        else:
            with make_server(args.host, args.port, app) as server:
                server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
