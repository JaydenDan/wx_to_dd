import asyncio

import signal
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, Tuple

from lxml import etree

from src.config import global_config
from src.utils.logger import setup_logger
from src.ding_talk.ddauto import DDAuto
from src.utils.wechat_dll_4_1_2_17 import RECV_CALLBACK, WeChatService, WeChatServiceHandler
from src.wechat.msg_handler import async_process_message

from loguru import logger
setup_logger(level=global_config.LOG_LEVEL)

LOADER_PATH = "Loader_4.1.2.17.dll"
HELPER_PATH = "Helper_4.1.2.17.dll"


def parse_reply_message(raw_xml: str) -> Tuple[Optional[str], Optional[str]]:
    """
    解析类型为49的XML消息，提取回复内容和原始内容。
    """
    if not raw_xml:
        return None, None
    try:
        root = etree.fromstring(raw_xml.encode("utf-8"))
        reply_content_element = root.find("./appmsg/title")
        reply_content = reply_content_element.text if reply_content_element is not None else None
        original_content_element = root.find("./appmsg/refermsg/content")
        original_content = original_content_element.text if original_content_element is not None else None
        return reply_content, original_content
    except etree.ParseError as exc:
        logger.error("XML 解析失败: {}", exc)
        return None, None


async def handle_incoming(message_type: int, data: dict, dd_sender):
    wx_type = data.get("wx_type")
    room_wxid = data.get("room_wxid") or ""
    listen_room = global_config.WECHAT_LISTEN_ROOM_WXID or ""
    # 如果配置了监听群，则只处理该群；未配置则默认全量（包括私聊）
    if listen_room and room_wxid != listen_room:
        return

    if wx_type == 1:
        msg = SimpleNamespace(type="text", content=data.get("msg"))
    elif wx_type == 49:
        reply_content, original_content = parse_reply_message(data.get("raw_msg"))
        msg = SimpleNamespace(type="quote", content=reply_content, quote_content=original_content)
    else:
        logger.debug("未处理的微信消息类型 wx_type={} message_type={}", wx_type, message_type)
        return

    logger.debug("分发消息 type={} content={}", msg.type, getattr(msg, "content", None))
    await async_process_message(msg=msg, chat=None, dd_sender=dd_sender)


class AppWeChatHandler(WeChatServiceHandler):
    def __init__(self, service, loop, dd_sender):
        super().__init__(service)
        self.loop = loop
        self.dd_sender = dd_sender

    @RECV_CALLBACK(in_class=True)
    def on_receive(self, client_id, message_type, data):
        # 回调在 DLL 线程中，尽快把工作切回 asyncio 事件循环
        self.loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                handle_incoming(message_type, data, self.dd_sender)
            )
        )


def build_dd_sender():
    if global_config.DINGTALK_SEND_METHOD == "local":
        target = global_config.DINGTALK_LOCAL_SEND_TARGET
        return DDAuto(target)
    return None


async def main():
    logger.info("starting wxhook 4.1.2.17 entry")
    loop = asyncio.get_running_loop()

    # 初始化 Playwright (提前启动浏览器)
    from src.utils.playwright_utils import PlaywrightManager
    logger.info("正在初始化 Playwright 浏览器环境...")
    # headless=False 方便调试和登录，生产环境可改为 True
    await PlaywrightManager.start(headless=True, check_login=True)

    # 如需启用本地钉钉发送，将下行改为 build_dd_sender()
    dd_sender = build_dd_sender()

    service = WeChatService(LOADER_PATH, HELPER_PATH)
    handler = AppWeChatHandler(service, loop, dd_sender)
    service.set_handler(handler)

    stop_event = asyncio.Event()

    def _stop(signum, frame):
        logger.info("收到信号 {}, 正在停止...", signum)
        stop_event.set()
        service.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except Exception:
            pass

    def _run_service():
        service.start()
        loop.call_soon_threadsafe(stop_event.set)

    thread = threading.Thread(target=_run_service, daemon=True)
    thread.start()

    await stop_event.wait()

    # 停止 Playwright
    await PlaywrightManager.stop()

    service.stop()
    if thread.is_alive():
        thread.join(timeout=5)

    logger.info("wxhook stopped.")


if __name__ == "__main__":
    asyncio.run(main())
