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


setup_logger(level=global_config.LOG_LEVEL)


def build_dd_sender():
    """
    根据配置构建钉钉发送器。
    """
    if global_config.DINGTALK_SEND_METHOD == "local":
        target = global_config.DINGTALK_LOCAL_SEND_TARGET
        return DDAuto(target)
    return None


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

    # 1. 过滤非目标事件 (只处理 2000 和 2004)
    event_type = msg_data.get("event_type")
    if str(event_type) not in ["2000", "2004"] and msg_data.get("msgType") is None:
        return {"code": 0, "msg": "success"}

    # 2. 获取发送者与房间信息
    from_user_name = msg_data.get("fromUserName", "")
    if isinstance(from_user_name, dict):
        from_user_name = from_user_name.get("String", "")
    from_user_name = str(from_user_name)
    
    room_wxid = from_user_name if from_user_name.endswith("@chatroom") else ""

    # 3. 过滤不关注的群聊
    listen_room = global_config.WECHAT_LISTEN_ROOM_WXID or ""
    if listen_room and room_wxid != listen_room:
        return {"code": 0, "msg": "success"}

    # 4. 解析真实内容 (判断引用消息还是普通文本)
    content_xml = msg_data.get("content_xml") or {}
    msg_node = content_xml.get("msg") or {}
    quote_title = msg_node.get("title") or (msg_node.get("appmsg") or {}).get("title")
    quote_refer_content = (msg_node.get("refermsg") or {}).get("content") or (msg_node.get("appmsg") or {}).get("refermsg", {}).get("content")
    real_content = msg_data.get("real_content")

    if not quote_title and not real_content:
        logger.debug(
            "未提取到有效文本或引用消息, event_type={} msgType={}",
            msg_data.get("event_type"),
            msg_data.get("msgType"),
        )
        return {"code": 0, "msg": "success"}

    # 5. 组装日志摘要
    event_desc = str(msg_data.get("event_desc") or msg_data.get("messageType") or "未知事件")
    sender_profile = msg_data.get("sender_profile", {})
    nickname = sender_profile.get("nickName", "") if isinstance(sender_profile, dict) else ""
    if not nickname:
        nickname = msg_data.get("sender_nick", "")
    sender = f"{nickname} [{from_user_name}]" if nickname else from_user_name

    if quote_title:
        summary = str(quote_title).replace("\n", " ")[:80]
        quoted = str(quote_refer_content or "").replace("\n", " ")[:50]
        logger.info(f"[收到 {event_desc}] | 发送者: {sender} | 内容: {summary} | 引用: {quoted}")
    else:
        summary = str(real_content).replace("\n", " ")[:80]
        logger.info(f"[收到 {event_desc}] | 发送者: {sender} | 内容: {summary}")

    # 6. 将原始字典数据分发给业务逻辑
    dd_sender = getattr(request.app.state, "dd_sender", None)
    asyncio.create_task(async_process_message(msg_data=msg_data, chat=None, dd_sender=dd_sender))
    return {"code": 0, "msg": "success"}
