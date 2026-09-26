from abc import ABC, abstractmethod


class ServerModule(ABC):
    """每个 Application 实例保存一个模块；资源必须支持并发请求。"""

    @abstractmethod
    def server_init(self):
        """Application 启动时初始化；保留旧方法名方便已有模块迁移。"""
        pass

    def server_close(self):
        """释放资源；即使 server_init 部分失败，也应能安全调用。"""
        pass
