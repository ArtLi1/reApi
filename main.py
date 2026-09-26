"""运行示例：python main.py，或 python main.py --server wsgiref。"""
import argparse
import logging
from wsgiref.simple_server import make_server

from HTTP import HTTPServer
from examples.basic import create_application


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", choices=("native", "wsgiref"), default="native")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    app = create_application()
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
