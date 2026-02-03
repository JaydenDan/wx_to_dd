from loguru import logger
import asyncio  # 1. 导入 asyncio
from cachetools import LRUCache

# --- 核心去重工具 ---
PROCESSED_URLS_CACHE = LRUCache(maxsize=256)
URL_CACHE_LOCK = asyncio.Lock()  # 2. 创建一个全局的异步锁

# 注意：函数现在是异步的 (async def)
async def is_url_processed(url: str) -> bool:
    """
    检查给定的URL是否已经被处理过。(线程安全版本)
    """
    async with URL_CACHE_LOCK:  # 3. 在访问缓存前获取锁
        if url in PROCESSED_URLS_CACHE:
            PROCESSED_URLS_CACHE[url] = True
            return True
        return False
    # 锁在这里会自动释放

# 注意：函数现在是异步的 (async def)
async def mark_url_as_processed(url: str):
    """
    将一个URL标记为已处理。(线程安全版本)
    """
    async with URL_CACHE_LOCK:  # 3. 在访问缓存前获取锁
        PROCESSED_URLS_CACHE[url] = True
        logger.debug("已将URL标记为已处理: {}", url)
    # 锁在这里会自动释放

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


