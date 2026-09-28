"""显式声明请求级依赖，并管理 yield 资源的生命周期。"""

import logging
from contextlib import ExitStack, contextmanager
from inspect import isasyncgenfunction, iscoroutinefunction, isgeneratorfunction


logger = logging.getLogger(__name__)


class Depends:
    def __init__(self, provider, *, use_cache=True):
        if (not callable(provider) or iscoroutinefunction(provider) or isasyncgenfunction(provider)
                or iscoroutinefunction(getattr(provider, "__call__", None))
                or isasyncgenfunction(getattr(provider, "__call__", None))):
            raise TypeError("Dependency provider must be a synchronous callable")
        try:
            hash(provider)
        except TypeError as error:
            raise TypeError("Dependency provider must be hashable") from error
        if type(use_cache) is not bool:
            raise TypeError("use_cache must be a bool")
        self.provider = provider
        self.use_cache = use_cache


class DependencyScope:
    def __init__(self, binder):
        self.binder = binder
        self.cache = {}
        self.stack = ExitStack()
        self.closed = False

    def resolve(self, marker, request):
        if self.closed:
            raise RuntimeError("Request dependency scope is closed")
        provider = marker.provider
        if marker.use_cache and provider in self.cache:
            return self.cache[provider]
        kwargs = self.binder.bind(request, provider, stage="dependency")
        # ExitStack 负责在请求结束时按获取顺序的反序释放资源。
        if isgeneratorfunction(provider) or isgeneratorfunction(getattr(provider, "__call__", None)):
            value = self.stack.enter_context(contextmanager(provider)(**kwargs))
        else:
            value = provider(**kwargs)
        if marker.use_cache:
            self.cache[provider] = value
        return value

    def close(self, error=None):
        if self.closed:
            return
        self.closed = True
        try:
            if error is None:
                self.stack.close()
            else:
                self.stack.__exit__(type(error), error, error.__traceback__)
        except Exception:
            # 响应可能已经发送；清理失败只能记录，不能改写既有响应。
            logger.exception("Request dependency cleanup failed")
        finally:
            self.cache.clear()
