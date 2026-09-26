from abc import ABC, abstractmethod


class ServerModule(ABC):
    """每个服务器保存一个模块实例；模块资源需要支持并发请求。"""

    @abstractmethod
    def server_init(self):
        """服务器开始监听前初始化资源。"""
        pass

    def server_close(self):
        """释放资源；即使 server_init 部分失败，也应能安全调用。"""
        pass
