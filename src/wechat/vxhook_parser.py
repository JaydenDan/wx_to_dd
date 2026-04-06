from types import SimpleNamespace
from typing import Optional, Tuple

from lxml import etree
from loguru import logger


VXHOOK_MESSAGE_EVENT_TYPES = {2000, 2004}


def extract_wechat_string(value) -> str:
    """
    提取 vxhook 回调中常见的 String 字段值。
    """
    if isinstance(value, dict):
        return value.get("String") or ""
    if value is None:
        return ""
    return str(value)


def parse_reply_message(raw_xml: str) -> Tuple[Optional[str], Optional[str]]:
    """
    解析引用消息 XML，提取回复内容和被引用内容。
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
        logger.error("引用消息 XML 解析失败: {}", exc)
        return None, None


def normalize_group_message_content(content: str, real_content: str) -> str:
    """
    规范化群聊原始内容。
    群聊回调里的 content.String 可能带有“发送者wxid:\\n”前缀，优先使用 hook 已裁剪好的 real_content。
    """
    if real_content:
        return real_content
    if ":\n" in content:
        return content.split(":\n", 1)[1]
    return content


def extract_quote_from_content_xml(payload: dict) -> Tuple[Optional[str], Optional[str]]:
    """
    优先从 hook 已结构化解析好的 content_xml 中提取引用消息内容。
    """
    content_xml = payload.get("content_xml") or {}
    msg_node = content_xml.get("msg") or {}
    appmsg = msg_node.get("appmsg") or {}
    refermsg = appmsg.get("refermsg") or {}
    if not appmsg or not refermsg:
        return None, None

    reply_content = appmsg.get("title")
    original_content = refermsg.get("content")
    return reply_content, original_content


def is_quote_message(payload: dict) -> bool:
    """
    判断当前回调是否为引用消息。
    新版 hook 外层通常是 msgType=49，内层 appmsg.type=57。
    """
    msg_type = str(payload.get("msgType") or "")
    forward_appmsg_type = str(payload.get("forward_appmsg_type") or "")
    content_xml = payload.get("content_xml") or {}
    appmsg = (content_xml.get("msg") or {}).get("appmsg") or {}
    appmsg_type = str(appmsg.get("type") or "")
    has_refermsg = bool(appmsg.get("refermsg"))

    return (
        msg_type == "57"
        or forward_appmsg_type == "57"
        or (msg_type == "49" and appmsg_type == "57")
        or has_refermsg
    )


def should_handle_vxhook_payload(payload: dict) -> bool:
    """
    判断当前回调是否属于需要处理的新版 hook 消息事件。
    """
    event_type = payload.get("event_type")
    if event_type is None:
        # 兼容少量不带 event_type 的调试场景，只要存在消息类型字段就继续尝试解析。
        return payload.get("msgType") is not None
    try:
        return int(event_type) in VXHOOK_MESSAGE_EVENT_TYPES
    except (TypeError, ValueError):
        return False


def build_message_from_vxhook_payload(payload: dict) -> Tuple[Optional[SimpleNamespace], str]:
    """
    把新版 vxhook HTTP 回调转换为当前业务可处理的消息对象。
    返回值为 (消息对象, room_wxid)。
    """
    from_user_name = extract_wechat_string(payload.get("fromUserName"))
    room_wxid = from_user_name if from_user_name.endswith("@chatroom") else ""
    msg_type = str(payload.get("msgType") or "")
    content = extract_wechat_string(payload.get("content"))
    real_content = payload.get("real_content") or ""
    normalized_content = normalize_group_message_content(content, real_content if room_wxid else "")

    if msg_type == "1":
        # 群聊优先使用 hook 已经裁好的 real_content，私聊继续使用原始 content。
        msg_content = normalized_content if room_wxid else content
        return SimpleNamespace(type="text", content=msg_content), room_wxid

    if is_quote_message(payload):
        reply_content, original_content = extract_quote_from_content_xml(payload)
        if not reply_content and not original_content:
            reply_content, original_content = parse_reply_message(normalized_content if room_wxid else content)
        if not reply_content:
            reply_content = normalized_content if room_wxid else (real_content or content)
        return (
            SimpleNamespace(
                type="quote",
                content=reply_content,
                quote_content=original_content,
            ),
            room_wxid,
        )

    return None, room_wxid


def describe_vxhook_payload(payload: dict) -> Tuple[str, str, str, Optional[str]]:
    """
    提取日志所需的消息摘要信息。
    返回值为 (事件描述, 发送者, 内容摘要, 引用摘要)。
    """
    event_desc = str(payload.get("event_desc") or payload.get("messageType") or "未知事件")
    from_user = extract_wechat_string(payload.get("fromUserName"))
    nickname = extract_wechat_string(payload.get("sender_profile", {}).get("nickName")) or payload.get("sender_nick") or ""
    content = extract_wechat_string(payload.get("content"))
    real_content = payload.get("real_content") or content
    sender = f"{nickname} [{from_user}]" if nickname else from_user
    if is_quote_message(payload):
        reply_content, original_content = extract_quote_from_content_xml(payload)
        summary = str(reply_content or "").replace("\n", " ")[:80]
        quoted = str(original_content or "").replace("\n", " ")[:50]
    else:
        summary = str(real_content).replace("\n", " ")[:80]
        quoted = None
    return event_desc, sender, summary, quoted
