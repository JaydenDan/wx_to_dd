import asyncio
import os
import re
import subprocess
import time
import functools
import inspect

from loguru import logger
from curl_cffi.requests import AsyncSession, RequestsError
import psutil

def timeit(enabled=True, log_level="DEBUG"):
    """
    高级装饰器：
    - 支持异步函数和同步函数
    - 输出格式为：xx s xxx ms
    - 支持自定义日志等级
    """
    def decorator(func):
        if not enabled:
            return func

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            start_time = time.time()
            try:
                return await func(*args, **kwargs)
            finally:
                end_time = time.time()
                elapsed_time = (end_time - start_time) * 1000
                # 使用 loguru 的格式化
                if log_level.upper() == "INFO":
                    logger.info("函数 [{}] 执行耗时: {:.2f} ms", func.__name__, elapsed_time)
                elif log_level.upper() == "DEBUG":
                    logger.debug("函数 [{}] 执行耗时: {:.2f} ms", func.__name__, elapsed_time)
                else:
                    logger.info("函数 [{}] 执行耗时: {:.2f} ms", func.__name__, elapsed_time)

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            start_time = time.time()
            try:
                return func(*args, **kwargs)
            finally:
                end_time = time.time()
                elapsed_time = (end_time - start_time) * 1000
                # 使用 loguru 的格式化
                if log_level.upper() == "INFO":
                    logger.info("函数 [{}] 执行耗时: {:.2f} ms", func.__name__, elapsed_time)
                elif log_level.upper() == "DEBUG":
                    logger.debug("函数 [{}] 执行耗时: {:.2f} ms", func.__name__, elapsed_time)
                else:
                    logger.info("函数 [{}] 执行耗时: {:.2f} ms", func.__name__, elapsed_time)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


def extract_author(msg: str) -> str:
    """
    根据新的消息格式，更精确地提取作者信息。

    Args:
        msg: 包含分享信息的完整字符串。

    Returns:
        提取到的作者用户名，如果未找到则返回空字符串。
    """
    user = None
    msg = msg.strip()

    # 优先检查消息中是否包含平台的关键词
    if 'xhslink.com' in msg or '小红书' in msg:
        # 小红书格式: 查找“发布了一篇...”前面的非空字符串
        # 例子: "... 38 小红薯663296B1发布了一篇..." -> 提取 "小红薯663296B1"
        match = re.search(r'(\S+)\s*发布了一篇小红书笔记', msg)
        if match:
            user = match.group(1)

    elif 'douyin.com' in msg or '抖音' in msg:
        # 抖音格式 1: 查找 "看看【xxx的yyy作品】"
        match = re.search(r'看看【(.+?)的(?:图文|视频)?作品】', msg)
        if match:
            user = match.group(1)
        else:
            # 抖音格式 2: 查找 "作者: xxx"
            match = re.search(r'作者\s*:\s*(.+)', msg)
            if match:
                user = match.group(1)

    elif 'weibo' in msg:
        # 微博逻辑保持不变
        match = re.search(r'作者：([^\s]+)', msg)
        if match:
            user = match.group(1)

    # 返回提取到的用户名，并去除可能存在的前后空白
    return user.strip() if user else ''