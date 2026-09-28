from typing import Optional

from HTTP import Depends
from HTTP.http_models import Path, Query, Body
from HTTP.http_module import ServerModule
from HTTP.http_request import HTTPRequest
from HTTP.http_response import HTTPResponse
from HTTP.http_application import HTTPApplication
from HTTP.http_server import HTTPServer
from Utils import ContentType

app = HTTPApplication()


class User:
    def __init__(self, a, b):
        self.a = a
        self.b = b

    def __str__(self):
        return f'User({self.a}, {self.b})'


class Module1(ServerModule):
    def __init__(self, a, b):
        self.a = a
        self.b = b

    def server_init(self):
        print("module1 success init")

    def l1(self):
        print("module1 func l1 success")


@app.router.before_handler("/**", 0)
def b1(req: HTTPRequest):
    return req


@app.router.after_handler("/**", 0)
def b2(req: HTTPRequest, res: HTTPResponse):
    return res


def d1():
    temp = 0

    def x():
        nonlocal temp
        print(temp)
        temp += 1

    return x


@app.router.get_method("/1/{id}/{uid}")
def f1(
        request: HTTPRequest,
        id: Path[int],
        q1: Query[int],
        module1: Module1,
        q2: Query[int] | None,
        q3: Optional[Query[int]],
        x1=Depends(d1,use_cache=True)
):
    x1()
    x1()
    return HTTPResponse(body=f"Hello World!",
                        content_type=ContentType.HTML)


@app.router.post_method("/2/{id}/{uid}")
def t2(request: HTTPRequest, id: Path[int], q1: Query[int], uid: Path[int], user: Body[User]):
    print(id.value)
    print(q1.value)
    print(uid.value)
    print(user.value)
    return HTTPResponse(body=f"Hello World!{request.path_params['id']},{request.path_params['uid']}",
                        content_type=ContentType.HTML)


# request = HTTPRequest("GET", "/1/2/3", "", {}, "")
# app.handle_request(request)
app.register_module(Module1, 1, 2)
if __name__ == "__main__":
    with app.lifecycle():
        HTTPServer(app).run()
