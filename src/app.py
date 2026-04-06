import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from loguru import logger

from src.config import global_config
from src.ding_talk.ddauto import DDAuto
from src.utils.file_cleaner import FileCleaner
from src.utils.logger import setup_logger
from src.utils.video_manager import video_manager
from src.wechat.msg_handler import async_process_message
from src.wechat.vxhook_parser import (
    build_message_from_vxhook_payload,
    describe_vxhook_payload,
    should_handle_vxhook_payload,
)


setup_logger(level=global_config.LOG_LEVEL)


def build_dd_sender():
    """
    根据配置构建钉钉发送器。
    """
    if global_config.DINGTALK_SEND_METHOD == "local":
        target = global_config.DINGTALK_LOCAL_SEND_TARGET
        return DDAuto(target)
    return None


async def dispatch_vxhook_payload(payload: dict, dd_sender):
    """
    把新版 vxhook 回调转换后交给现有业务链路处理。
    """
    msg, room_wxid = build_message_from_vxhook_payload(payload)
    if not msg:
        logger.debug(
            "未处理的 vxhook 消息类型 event_type={} msgType={} event_desc={}",
            payload.get("event_type"),
            payload.get("msgType"),
            payload.get("event_desc"),
        )
        return

    listen_room = global_config.WECHAT_LISTEN_ROOM_WXID or ""
    if listen_room and room_wxid != listen_room:
        return

    await async_process_message(msg=msg, chat=None, dd_sender=dd_sender)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    管理应用启动与关闭时的公共资源。
    """
    logger.info("starting vxhook fastapi app")

    video_manager.init_client()

    cleaner = FileCleaner(interval_seconds=60, max_age_seconds=300)
    cleaner.start()
    app.state.cleaner = cleaner

    from src.utils.playwright_utils import PlaywrightManager

    logger.info("正在初始化 Playwright 浏览器环境...")
    await PlaywrightManager.start(headless=True, check_login=True)
    app.state.playwright_manager = PlaywrightManager

    app.state.dd_sender = build_dd_sender()

    try:
        yield
    finally:
        await PlaywrightManager.stop()
        cleaner.stop()
        logger.info("vxhook fastapi app stopped")


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    """
    提供基础健康检查接口。
    """
    return {"code": 1, "msg": "ok"}


@app.post("/api/recvMsg")
@app.put("/api/recvMsg")
async def receive_message(request: Request):
    """
    接收新版 vxhook 推送的 HTTP 消息。
    """
    try:
        msg_data = await request.json()
    except Exception as exc:
        logger.error("解析 vxhook 回调请求失败: {}", exc)
        return {"code": -1, "msg": "invalid json"}

    if not should_handle_vxhook_payload(msg_data):
        return {"code": 0, "msg": "success"}

    event_desc, sender, summary, quoted = describe_vxhook_payload(msg_data)
    if quoted:
        logger.info("[收到 {}] | 发送者: {} | 内容: {} | 引用: {}", event_desc, sender, summary, quoted)
    else:
        logger.info("[收到 {}] | 发送者: {} | 内容: {}", event_desc, sender, summary)

    dd_sender = getattr(request.app.state, "dd_sender", None)
    asyncio.create_task(dispatch_vxhook_payload(msg_data, dd_sender))
    return {"code": 0, "msg": "success"}
