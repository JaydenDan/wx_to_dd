import asyncio
import time

from curl_cffi.requests import AsyncSession

from loguru import logger

from src.config import global_config
from src.utils.deduplication import mark_url_as_processed, mark_username_as_processed
from src.utils.commons import extract_author

headers = {
    'Content-Type': 'application/json',
    'Accept': '*/*',
    'Connection': 'keep-alive'
}
async def send_to_dd(msg: str):
    start_time = time.time()
    payload = {
        'id': global_config.DINGTALK_HOOK_SEND_TARGET,
        "msg": msg.replace("https://", "https:// ")
    }
    logger.info("开始发送钉钉消息")
    async with AsyncSession() as session:
        response = await session.post(
            global_config.DINGTALK_HOOK_URL,
            headers=headers,
            json=payload,  # 直接使用json参数而不是手动dumps
            timeout=2  # 减少超时时间
        )
    logger.info(f'钉钉 Hook 调用耗时 {int((time.time() - start_time) * 1000)} ms')
    if response.status_code == 200:
        logger.info("钉钉消息发送成功")
        match_obj = global_config.URL_PATTERN.search(msg)
        url = match_obj.group(0)
        await mark_url_as_processed(url)
        username = extract_author(msg)
        await mark_username_as_processed(username)
    else:
        logger.error(f"钉钉消息发送失败: {response.status_code}, {response.text}")
