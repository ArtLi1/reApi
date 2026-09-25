from typing import Generic, TypeVar

T = TypeVar("T")

# query path仅支持int str bool float四种类型 body要求contentType为application/json 且body为合法json字符串
class Path(Generic[T]):
    def __init__(self, value: T):
        super().__init__(value)


class Query(Generic[T]):
    def __init__(self, value: T):
        super().__init__(value)


class Body(Generic[T]):
    def __init__(self, value: T):
        super().__init__(value)


