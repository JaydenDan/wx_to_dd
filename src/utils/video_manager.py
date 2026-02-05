from typing import Optional
from videodl import videodl
from loguru import logger

class VideoManager:
    _instance = None
    _client = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(VideoManager, cls).__new__(cls)

        return cls._instance

    def init_client(self):
        """初始化 VideoClient"""
        try:
            logger.info("正在初始化 VideoClient...")
            self._client = videodl.VideoClient(
                allowed_video_sources=['WeiboVideoClient', 'DouyinVideoClient', 'KuaishouVideoClient', 'RednoteVideoClient']
            )
            logger.info("VideoClient 初始化成功")
        except Exception as e:
            logger.error(f"VideoClient 初始化失败: {e}")
            raise e

    @property
    def client(self) -> Optional[videodl.VideoClient]:
        if self._client is None:
            logger.warning("VideoClient 尚未初始化，尝试初始化...")
            try:
                self.init_client()
            except Exception:
                return None
        return self._client

# 全局单例
video_manager = VideoManager()
