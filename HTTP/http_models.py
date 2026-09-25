from typing import Generic, TypeVar

T = TypeVar("T")


class BaseParam(Generic[T]):
    def __init__(self, value: T):
        self.value = value


class Path(BaseParam[T]):
    def __init__(self, value: T):
        super().__init__(value)


class Query(BaseParam[T]):
    def __init__(self, value: T):
        super().__init__(value)


class Body(BaseParam[T]):
    def __init__(self, value: T):
        super().__init__(value)


