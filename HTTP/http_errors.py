class HTTPError(Exception):
    code = 500
    status = "Internal Server Error"
    error_code = "http_error"


class RouteNotFoundError(HTTPError):
    code = 404
    status = "Not Found"
    error_code = "route_not_found"


class MethodNotAllowedError(HTTPError):
    code = 405
    status = "Method Not Allowed"
    error_code = "method_not_allowed"

    def __init__(self, message, allowed_methods):
        super().__init__(message)
        self.allowed_methods = tuple(allowed_methods)
        self.headers = {"Allow": ", ".join(self.allowed_methods)}


class ParameterValidationError(HTTPError):
    """统一参数错误结构；不在响应中附带原始输入或校验器上下文。"""
    code = 400
    status = "Bad Request"
    error_code = "validation_error"
    source = "body"

    def __init__(self, message, *, location=None, error_type="invalid_parameter", errors=None):
        super().__init__(message)
        self.errors = errors if errors is not None else [{
            "location": list(location if location is not None else (self.source,)),
            "message": message,
            "type": error_type,
        }]


class PathParameterError(ParameterValidationError):
    source = "path"


class QueryParameterError(ParameterValidationError):
    source = "query"


class BodyParameterError(ParameterValidationError):
    source = "body"
