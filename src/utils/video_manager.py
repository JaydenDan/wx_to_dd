import threading
from typing import Any, Optional

from videodl import videodl
from loguru import logger


_SUPPORTED_PLATFORM_SOURCES = (
    "WeiboVideoClient",
    "DouyinVideoClient",
    "KuaishouVideoClient",
    "RednoteVideoClient",
)


class VideoManager:
    _instance = None
    _client = None
    _parser_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(VideoManager, cls).__new__(cls)

        return cls._instance

    def init_client(self):
        """初始化 VideoClient"""
        try:
            logger.info("正在初始化 VideoClient...")
            self._client = videodl.VideoClient(
                allowed_video_sources=list(_SUPPORTED_PLATFORM_SOURCES),
                init_video_clients_cfg={
                    **{
                        source: {"disable_print": True}
                        for source in _SUPPORTED_PLATFORM_SOURCES
                    },
                    # Generic parsing is only a last-resort fallback. Keep its
                    # diagnostics in videodl.log and avoid retry storms in case
                    # another caller reaches it.
                    "WebMediaGrabber": {
                        "disable_print": True,
                        "max_retries": 1,
                        "browser_headless": True,
                    }
                },
            )
            logger.info("VideoClient 初始化成功")
        except Exception as e:
            logger.error(f"VideoClient 初始化失败: {e}")
            raise e

    @staticmethod
    def _find_platform_client(client: Any, url: str) -> tuple[Optional[str], Any]:
        """Find the built-in platform parser without constructing generic parsers."""
        for source, entry in list(getattr(client, "video_clients", {}).items()):
            parser_class = entry.get("cls") if isinstance(entry, dict) else type(entry)
            belongto = getattr(parser_class, "belongto", None)
            if not callable(belongto):
                continue
            try:
                if belongto(url):
                    return source, entry
            except Exception:
                continue
        return None, None

    def parse_video_url(self, url: str) -> list[Any]:
        """Parse a message URL without opening videodl's generic browser fallback.

        The application only forwards videos from the built-in platform clients.
        When one of those parsers cannot resolve a video, returning an empty list
        is preferable to letting ``VideoClient`` launch WebMediaGrabber, which
        probes many CDN candidates and may start a separate Chromium window.
        Direct media URLs remain supported through WebMediaGrabber's request-only
        path because that path does not need a browser.
        """
        client = self.client
        if not client:
            return []

        source, entry = self._find_platform_client(client, url)
        if source is not None:
            try:
                if isinstance(entry, dict):
                    from videodl.modules import BuildVideoClient

                    with self._parser_lock:
                        current_entry = client.video_clients.get(source)
                        if isinstance(current_entry, dict):
                            current_entry = BuildVideoClient(module_cfg=current_entry["cfg"])
                            client.video_clients[source] = current_entry
                        entry = current_entry
                request_overrides = getattr(client, "requests_overrides", {}).get(source, {})
                return entry.parsefromurl(url, request_overrides=request_overrides) or []
            except Exception as exc:
                logger.warning("视频平台解析失败，已跳过通用浏览器兜底: {}", exc)
                return []

        # A direct media URL can still be downloaded without a page parser.
        web_media_grabber = getattr(client, "web_media_grabber", None)
        if web_media_grabber is not None:
            try:
                request_overrides = getattr(client, "requests_overrides", {}).get(
                    getattr(web_media_grabber, "source", "WebMediaGrabber"), {}
                )
                is_direct, _ = web_media_grabber.isprobablydirectmedia(
                    url, request_overrides=request_overrides
                )
                if is_direct:
                    return web_media_grabber.parsefromurl(
                        url, request_overrides=request_overrides
                    ) or []
            except Exception as exc:
                logger.debug("直链视频解析失败: {}", exc)

        logger.debug("未匹配内置视频平台，跳过通用 WebMediaGrabber: {}", url)
        return []

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
