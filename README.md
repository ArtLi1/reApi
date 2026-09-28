# httpDemo

轻量同步 Python Web 框架和多线程 WSGI Server。支持 Python 3.10+；网络层使用标准库，请求体模型校验使用 Pydantic 2。

## 运行

```bash
python -m pip install -r requirements.txt
python main.py
# 或使用标准库 WSGI Server
python main.py --server wsgiref
```

示例接口：`GET /`、`GET /hello/Alice?tag=demo`、`POST /echo`（JSON 请求体 `{"text":"hello"}`）。

## 最小应用

```python
from HTTP import Depends, HTTPApplication, HTTPRequest, HTTPResponse, HTTPServer, Path, Query

app = HTTPApplication()

def trace_id(request: HTTPRequest):
    return getattr(request.state, "request_id", "anonymous")

@app.router.get_method("/hello/{name}")
def hello(name: Path[str], tag: Query[str] | None, trace=Depends(trace_id)):
    return HTTPResponse(f"Hello {name.value}; tag={tag.value if tag else None}; id={trace}")

if __name__ == "__main__":
    with app.lifecycle():
        HTTPServer(app).run()
```

`HTTPApplication` 是 WSGI callable，可交给其他 WSGI Server；`HTTPServer` 也可运行普通 WSGI callable。应用启动与关闭需由入口显式调用 `startup()/shutdown()` 或 `lifecycle()`；多进程部署时，在各工作进程中分别创建应用并启动资源。

## 请求处理

```text
Middleware 进入 → 解析请求体 → 匹配路由 → before Hook
→ 绑定 Handler 和 after 参数 → Handler → after Hook
→ Middleware 返回 → WSGI 响应迭代与关闭
```

- 路由支持 `GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS`、多方法声明和 `include_router`。最具体路径先匹配，再判断方法；无路由返回 404，方法不匹配返回 405 和 `Allow`。HEAD 可复用 GET；未显式声明的 OPTIONS 自动返回 204。
- `Path[T]`、`Query[T]` 仅支持 `str/int/float/bool`，使用 `.value` 读取。 `Body[Model]` 仅接收 JSON 对象，按 Pydantic 严格校验；支持 BaseModel、dataclass 和普通构造类。 `T | None` 缺失时注入 `None`，存在但无效仍返回 400。
- `before_handler(path, priority)` 与 `after_handler(path, priority)` 的路径只允许 `*`、`**` 通配段；优先级越小越先执行，同级按注册顺序执行。after 可接收 `HTTPResponse` 并修改响应。
- `@app.middleware` 接收 `(request, call_next)`，可短路请求或包装响应；`call_next()` 在当前调用中只能执行一次。每个请求有独立的 `request.state`。
- 参数错误返回包含 `code/message/errors` 的 JSON；`@app.exception_handler(ErrorType)` 可注册同步异常处理器，签名为 `(request, error)`。

## 请求级依赖与应用级模块

`Depends(provider)` 可用于 Handler、before/after Hook，也可嵌套在其他 provider 中。provider 使用同样的 Request、Path、Query、Body 和模块注入规则；依赖链在路由注册时检查，包括循环依赖。默认同一 provider 在一个请求中只执行一次；`Depends(provider, use_cache=False)` 每次都重新创建。

```python
import sqlite3
from HTTP import Depends, ServerModule

class DatabaseConfig(ServerModule):
    def server_init(self):
        self.path = ":memory:"

app.register_module(DatabaseConfig)

def connection(config: DatabaseConfig):
    db = sqlite3.connect(config.path)
    try:
        yield db
    finally:
        db.close()

@app.router.get_method("/db")
def ping(db=Depends(connection)):
    return HTTPResponse(str(db.execute("SELECT 1").fetchone()[0]))
```

`ServerModule` 在应用启动时初始化，并由所有请求共享；共享资源须支持并发访问。`yield` provider 在请求中取得资源，清理按依赖获取的逆序执行。直接调用 `handle_request/dispatch_request` 时，返回前清理；WSGI 调用在响应迭代结束或服务器关闭迭代器时清理，连接提前断开也会触发关闭。Handler 或依赖报错时立即清理。当前仅支持同步 provider 和缓冲的框架响应。

## 协议范围

自带 Server 支持 HTTP/1.0、HTTP/1.1 的单请求连接、线程池及读取期限；每个连接响应后关闭。默认 Header 上限 64 KiB、Body 上限 10 MiB、总读取期限 10 秒。不支持 TLS、Keep-Alive 或 chunked 请求体；外部 WSGI Server 的网络限制由外部配置管理。
