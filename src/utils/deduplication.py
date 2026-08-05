import asyncio
import re
from urllib.parse import urljoin, urlparse

from cachetools import LRUCache
from curl_cffi.requests import AsyncSession
from loguru import logger

# --- 核心去重工具 ---
PROCESSED_URLS_CACHE = LRUCache(maxsize=256)
URL_CACHE_LOCK = asyncio.Lock()  # 2. 创建一个全局的异步锁

_URL_KEY_CACHE = LRUCache(maxsize=512)
_URL_KEY_CACHE_LOCK = asyncio.Lock()
_XHS_SHORT_HOSTS = {"xhslink.cn", "xhslink.com"}
_XHS_NOTE_ID_PATTERN = re.compile(
    r"/(?:explore|discovery/item|item)/([0-9a-zA-Z]+)(?:/|$)",
    re.IGNORECASE,
)
_URL_TRAILING_CHARS = " \t\r\n,，。！？；：、)]}》」』"


def _normalize_url(url: str) -> str:
    """去掉消息文本中常见的 URL 尾随标点，避免同一链接产生多个键。"""
    return str(url or "").strip().rstrip(_URL_TRAILING_CHARS)


def _xhs_note_id(url: str) -> str | None:
    """从完整小红书页面地址中提取稳定的笔记 ID。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    hostname = (parsed.hostname or "").lower()
    if not (
        hostname == "xiaohongshu.com"
        or hostname.endswith(".xiaohongshu.com")
    ):
        return None

    match = _XHS_NOTE_ID_PATTERN.search(parsed.path)
    return match.group(1).lower() if match else None


async def _resolve_xhs_short_url(url: str) -> str | None:
    """从短链 302 的 discovery/item 地址提取笔记 ID，不等待前端切到 explore。"""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        current_url = url
        async with AsyncSession() as session:
            # 通常只有一步 302；保留三步用于兼容可能出现的 http -> https 跳转。
            for _ in range(3):
                response = await asyncio.wait_for(
                    session.get(
                        current_url,
                        headers=headers,
                        allow_redirects=False,
                        timeout=6,
                    ),
                    timeout=8,
                )

                location = response.headers.get("location")
                if location:
                    redirect_url = urljoin(current_url, location)
                    note_id = _xhs_note_id(redirect_url)
                    if note_id:
                        return note_id

                    redirect_host = (urlparse(redirect_url).hostname or "").lower()
                    if redirect_host in _XHS_SHORT_HOSTS:
                        current_url = redirect_url
                        continue
                    return None

                # 某些网关可能返回 HTML 而不是 302，尽量从页面链接提取。
                body = response.text[:1_000_000]
                match = re.search(
                    r"https?://(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item|item)/[0-9a-zA-Z]+",
                    body,
                    re.IGNORECASE,
                )
                return _xhs_note_id(match.group(0)) if match else None

        return None
    except Exception as exc:
        logger.debug("小红书短链解析失败，暂按原链接去重: {} ({})", url, exc)
        return None


async def get_url_dedup_key(url: str) -> str:
    """返回用于去重的稳定键，小红书短链统一到真实笔记 ID。"""
    normalized_url = _normalize_url(url)
    if not normalized_url:
        return ""

    note_id = _xhs_note_id(normalized_url)
    if note_id:
        return f"xhs:{note_id}"

    try:
        parsed = urlparse(normalized_url)
        hostname = (parsed.hostname or "").lower()
    except ValueError:
        hostname = ""

    if hostname not in _XHS_SHORT_HOSTS:
        return normalized_url

    async with _URL_KEY_CACHE_LOCK:
        cached_key = _URL_KEY_CACHE.get(normalized_url)
    if cached_key:
        return cached_key

    resolved_note_id = await _resolve_xhs_short_url(normalized_url)
    dedup_key = f"xhs:{resolved_note_id}" if resolved_note_id else normalized_url
    if resolved_note_id:
        async with _URL_KEY_CACHE_LOCK:
            _URL_KEY_CACHE[normalized_url] = dedup_key
        logger.debug("小红书短链已归一化: {} -> {}", normalized_url, dedup_key)
    return dedup_key

# 注意：函数现在是异步的 (async def)
async def is_url_processed(url: str) -> bool:
    """
    检查给定的URL是否已经被处理过。(线程安全版本)
    """
    key = await get_url_dedup_key(url)
    if not key:
        return False
    async with URL_CACHE_LOCK:
        if key in PROCESSED_URLS_CACHE:
            PROCESSED_URLS_CACHE[key] = True
            return True
        return False
    # 锁在这里会自动释放


async def claim_url_if_not_processed(url: str) -> bool:
    """
    原子地检查并占用一个 URL。
    返回 True 表示本次成功占坑，可继续处理；
    返回 False 表示该 URL 已在缓存中，应跳过。
    """
    key = await get_url_dedup_key(url)
    if not key:
        return True

    async with URL_CACHE_LOCK:
        if key in PROCESSED_URLS_CACHE:
            PROCESSED_URLS_CACHE[key] = True
            return False
        PROCESSED_URLS_CACHE[key] = True
        logger.debug("已原子占用URL: {} (去重键: {})", url, key)
        return True

# 注意：函数现在是异步的 (async def)
async def mark_url_as_processed(url: str):
    """
    将一个URL标记为已处理。(线程安全版本)
    """
    key = await get_url_dedup_key(url)
    if not key:
        return
    async with URL_CACHE_LOCK:
        PROCESSED_URLS_CACHE[key] = True
        logger.debug("已将URL标记为已处理: {} (去重键: {})", url, key)
    # 锁在这里会自动释放


async def release_claimed_url(url: str):
    """
    释放之前占用但最终发送失败的 URL。
    """
    key = await get_url_dedup_key(url)
    if not key:
        return

    async with URL_CACHE_LOCK:
        if key in PROCESSED_URLS_CACHE:
            del PROCESSED_URLS_CACHE[key]
            logger.debug("已释放占用URL: {} (去重键: {})", url, key)

# --- 新增：用户名去重逻辑 ---
PROCESSED_USERNAMES_CACHE = LRUCache(maxsize=64)  # 您可以为用户名设置不同的大小
USERNAME_CACHE_LOCK = asyncio.Lock()  # 为用户名缓存创建一把独立的锁

async def is_username_processed(username: str) -> bool:
    """
    检查给定的用户名是否已经被处理过。(异步安全版本)
    """
    async with USERNAME_CACHE_LOCK:
        if username in PROCESSED_USERNAMES_CACHE:
            # 命中缓存，更新其在LRU中的位置
            PROCESSED_USERNAMES_CACHE[username] = True
            return True
        return False

async def mark_username_as_processed(username: str):
    """
    将一个用户名标记为已处理。(异步安全版本)
    """
    async with USERNAME_CACHE_LOCK:
        PROCESSED_USERNAMES_CACHE[username] = True
        logger.debug("已将用户名标记为已处理: {}", username)


