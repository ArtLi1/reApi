# httpDemo

轻量同步 Python Web 框架及线程池 WSGI Server，Python 3.10+，仅使用标准库。

## 启动示例

```bash
python main.py
python main.py --server wsgiref
```

两个入口使用相同的 Application。示例接口：

- `GET /`：框架信息。
- `GET /hello/Alice?tag=demo`：Path、可选 Query、共享模块注入。
- `POST /echo`：`Content-Type: application/json`，请求体 `{"text":"hello"}`。

## 框架应用

```python
from HTTP import HTTPApplication, HTTPResponse, HTTPServer, Path, Query

app = HTTPApplication()

@app.router.get_method("/hello/{name}")
def hello(name: Path[str], tag: Query[str] | None):
    return HTTPResponse(f"Hello {name.value}; tag={None if tag is None else tag.value}")

if __name__ == "__main__":
    with app.lifecycle():
        HTTPServer(app).run()
```

`Application.__call__(environ, start_response)` 是标准 WSGI 应用入口。也可在
`app.lifecycle()` 上下文中将 `app` 交给 `wsgiref.simple_server.make_server()`。
外部多进程服务器应在每个工作进程中创建、启动和关闭 Application，不能依赖
WSGI 自动调用生命周期方法，也不应在 fork 前创建模块连接。

## 运行普通 WSGI 应用

```python
from HTTP import HTTPServer

def application(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"Hello WSGI"]

HTTPServer(application).run()
```

Server 只调用 WSGI 接口，包装后的中间件无需暴露路由或模块管理方法。

## 职责与生命周期

| 组件 | 职责 |
|---|---|
| `http_protocol` | HTTP 报文边界、长度校验、读取期限与大小限制 |
| `http_server` | Socket、线程池、背压、等待请求排空 |
| `http_wsgi` | environ、start_response、字节迭代、发送与关闭 |
| `http_application` | 模块、应用生命周期、请求流程、框架异常响应 |
| `http_router` | 路由与 Hook 注册、路径匹配 |
| `http_parameters` | 声明检查与统一参数绑定 |
| `http_request` / `http_response` | 框架请求、响应数据与 WSGI 转换 |

启动入口执行 `app.startup()`，启动失败会逆序释放已尝试初始化的模块。
停止服务器并等待 WSGI 响应迭代结束之后执行 `app.shutdown()`；关闭可重复调用，
同一应用实例仅允许启动一次。`app.lifecycle()` 封装这个顺序。

模块继续继承 `ServerModule` 并实现 `server_init()` / `server_close()`，由
Application 管理，按注册类注入同一个实例。共享模块需要支持并发访问。

`app.dispatch_request(request)` 保留异常，便于针对内部行为测试；
`app.handle_request(request)` 将处理异常转为响应。WSGI 入口还覆盖请求构造时的错误。

## 路由注册与分组

```python
from HTTP import HTTPApplication, HTTPRouter, HTTPResponse, Path

app = HTTPApplication()
items = HTTPRouter(prefix="/items")

@items.route("/{id}", methods=["GET", "PATCH"])
def item(id: Path[int]):
    return HTTPResponse(str(id.value))

@items.after_handler("/**", priority=0)
def mark(response: HTTPResponse):
    response.headers["X-Group"] = "items"

app.router.include_router(items, prefix="/api/v1")
# 实际路径：/api/v1/items/{id}；Hook 同时挂载到 /api/v1/items/**。
```

- `route(path, methods=("GET",))` 支持多个方法，方法名统一为大写；
  `methods` 应为列表或元组等可迭代集合，不能直接传字符串。
- 保留 `get_method`、`post_method`、`put_method`、`delete_method`；
  新增 `patch_method`、`head_method`、`options_method`。
- prefix 只能包含字面路径段；支持嵌套 include，多层 prefix 依次拼接。
- include 复制子路由当时的路由和 Hook，后续注册不会自动同步；模块由最终 Application
  注册和注入。相同优先级的 Hook 按父路由实际注册/挂载顺序执行。
- 路径以 `/` 开始，参数和 `*` / `**` 必须占据完整段；参数名为 ASCII 标识符且不能重复。
  不允许查询串、片段、控制字符及空路径段。非法声明在注册时失败。
- 末尾 `/` 与无末尾 `/` 等价；同一方法下的重复或同形路径直接报错，
  如 `/items/{id}`、`/items/{name}`、`/items/*`。不同方法可使用不同参数名。
- 匹配时从左到右比较路径段：字面段优先于单段参数/`*`，再优先于 `**`；
  完整路径优先于继续匹配的通配路径。具体程度完全相同的重叠模式按资源首次注册顺序处理。
- **先选最具体的路径资源，再选择方法。** 若 GET `/items/new` 与 POST `/items/{id}`
  同时存在，POST `/items/new` 返回 405，避免落入动态 Handler。
- 所有路由、Hook 和 include 操作都必须在应用启动前完成。

### HTTP 方法行为

| 请求情况 | 行为 |
|---|---|
| 路径不存在 | 404 |
| 路径存在、方法不支持 | 405，携带 `Allow` |
| HEAD | 显式 HEAD 优先，否则复用 GET；Handler 仍收到 HEAD 方法 |
| 自动 OPTIONS | 返回 204 和 `Allow`，跳过 Handler、路由 Hook 和业务参数校验 |
| 显式 OPTIONS | 执行正常 Handler / Hook 流程，响应由 Handler 定义 |

`Allow` 按名称排序，包含显式方法、自动 OPTIONS，以及 GET 对应的 HEAD。
HEAD 的 Hook 处理完整 Response；Application 在 WSGI 输出时去掉正文，保留正常响应的
`Content-Length`，包括错误响应。内部 dispatch / handle 返回的 Response 仍保留正文。
自动 OPTIONS 仅提供方法能力信息；CORS 响应头需要由应用另行定义。
方法语义参考 [RFC 9110](https://httpwg.org/specs/rfc9110.html)。

## 参数与 Hook

- `HTTPRequest`、模块类型：Handler、before、after 均可注入。
- `HTTPResponse`：仅 after 可注入；多个 after 收到最新响应。
- `Path[T]` / `Query[T]`：T 为 str、int、float、bool；包装值通过 `.value` 获取。
- `Body[Model]`：仅 JSON 对象，以 `Model(**json)` 构造；普通模型注解不会自动验证字段类型。
- `T | None` / `Optional[T]`：缺失时注入 None，有值时正常校验，非法值仍报错。
- Hook 路径只支持完整的 `*` / `**` 通配段；优先级越小越先执行，相同值按注册顺序。
- 流程：匹配路由 → before → Handler 与 after 参数预校验 → Handler → after。
- before 可修改 query、path_params、json；不能改变已经匹配的请求方法或路径。
- after 仅在 Handler 成功返回后执行；Body 模型构造函数应只处理数据。

## 协议范围

自研 Server 接收 HTTP/1.0 与 HTTP/1.1 的 origin-form 请求；每个连接处理一次请求，
输出 `Connection: close`，不提供 Keep-Alive、请求流水线或 TLS。
第一版不解码 chunked 请求，遇到 Transfer-Encoding 返回 501；与 Content-Length
冲突、重复长度、截断请求返回 400。支持 `Expect: 100-continue`。

默认 Header 上限 64 KiB、Body 上限 10 MiB、请求总读取期限 10 秒，可由 Server 参数配置。
此超时约束网络读取，不会取消正在执行的 Handler。外部 WSGI Server 的读取限制由外部配置管理。

Server 支持生成器、write()、空输出、重复响应头、HEAD、无 Body 状态和 file_wrapper；
结束、异常或断开时关闭响应迭代器。响应已开始发送后的错误只记录并终止连接。
框架自身当前提供缓冲的 bytes 响应，after 修改 Body 后重新计算输出长度。
重复响应头可使用 `response.add_header("Set-Cookie", "a=1")`。

路径按 WSGI 的 Latin-1 字符串承载字节，再按 UTF-8 解释；路由匹配已解码路径，
Path 不会二次解码。`SCRIPT_NAME` 是挂载前缀，路由匹配 `PATH_INFO`。
例如 `%252F` 得到字面值 `%2F`；`%2F` 会成为路径分隔符，不能跨标准 WSGI 入口保证
恢复原始 URL 编码。响应头与状态遵循 WSGI 的 Latin-1 编码规则，正文可使用 UTF-8。

## 迁移与验证

旧 `server.router`、`server.register_module()`、`server.get_module()` 分别迁到
`app.router`、`app.register_module()`、`app.get_module()`；Server 构造时传入 WSGI callable。
启动入口显式包裹 `app.lifecycle()`，不会由 Server 猜测应用生命周期。

```bash
python -m test.run_checks
```

测试保存在 `test/` 并纳入版本管理，包括两端互操作、wsgiref.validate、并发请求、
模块回滚与关闭、Hook 顺序、可选参数、响应迭代和协议边界。
