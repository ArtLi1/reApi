class HTTPError(Exception):
    code = 500
    status = "Internal Server Error"


class RouteNotFoundError(HTTPError):
    code = 404
    status = "Not Found"


class MethodNotAllowedError(HTTPError):
    code = 405
    status = "Method Not Allowed"

    def __init__(self, message, allowed_methods):
        super().__init__(message)
        self.allowed_methods = tuple(allowed_methods)
        self.headers = {"Allow": ", ".join(self.allowed_methods)}


class PathParameterError(HTTPError):
    code = 400
    status = "Bad Request"


class QueryParameterError(HTTPError):
    code = 400
    status = "Bad Request"


class BodyParameterError(HTTPError):
    code = 400
    status = "Bad Request"
