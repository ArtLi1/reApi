from HTTP.http_models import Path, Query, Body
from HTTP.http_module import ServerModule
from HTTP.http_request import HTTPRequest
from HTTP.http_response import HTTPResponse
from HTTP.http_server import HTTPServer
from Utils import ContentType

server = HTTPServer()


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
        print(self.a, self.b)


@server.router.before_handler("/**", 0)
def b1(req: HTTPRequest):
    req.path_params["id"] = "-2"
    return req


@server.router.after_handler("/**", 0)
def b2(req: HTTPRequest, res: HTTPResponse):
    return res


@server.router.get_method("/1/{id}/{uid}")
def f1(request: HTTPRequest, id: Path[int], uid: Path[int]):
    print(id.value)
    print(uid.value)
    print(request.path_params["id"])
    return HTTPResponse(body=f"Hello World!{request.path_params["id"]},{request.path_params["uid"]}",
                        content_type=ContentType.HTML)


@server.router.post_method("/2/{id}/{uid}")
def t2(request: HTTPRequest, id: Path[int], q1: Query[int], uid: Path[int], user: Body[User]):
    print(id.value)
    print(q1.value)
    print(uid.value)
    print(user.value)
    return HTTPResponse(body=f"Hello World!{request.path_params["id"]},{request.path_params["uid"]}",
                        content_type=ContentType.HTML)


# request = HTTPRequest("GET", "/1/2/3", "", {}, "")
# server.router.router(request)
server.register_module(Module1, 1, 2)
server.run()
