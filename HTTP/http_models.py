from typing import Generic, TypeVar

T = TypeVar("T")


class BaseParam(Generic[T]):
    def __init__(self, value: T):
        self.value = value


# Path 和 Query 仅支持 str、int、float、bool；Body 绑定 JSON 对象。
class Path(BaseParam[T]):
    pass


class Query(BaseParam[T]):
    pass


class Body(BaseParam[T]):
    pass
