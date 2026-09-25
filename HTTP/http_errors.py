class HTTPError(Exception):
    code = 500
    status = "Internal Server Error"


class RouteNotFoundError(HTTPError):
    code = 404
    status = "Not Found"


class PathParameterError(HTTPError):
    code = 400
    status = "Bad Request"


class QueryParameterError(HTTPError):
    code = 400
    status = "Bad Request"


class BodyParameterError(HTTPError):
    code = 400
    status = "Bad Request"
