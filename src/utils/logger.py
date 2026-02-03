import os
import sys
import logging
from loguru import logger

# 拦截标准库 logging 的 Handler
class InterceptHandler(logging.Handler):
    def emit(self, record):
        # 获取对应的 Loguru level
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # 查找调用者的帧，以便 loguru 能正确显示文件名和行号
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )

def setup_logger(level: str = "INFO", log_dir: str = "logs", log_filename: str = "app.log"):
    """
    配置 loguru 日志系统，并拦截标准库 logging 日志
    """
    # 移除默认的 handler
    logger.remove()

    # 1. 控制台输出
    logger.add(
        sys.stdout,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    )

    # 2. 文件输出
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    log_path = os.path.join(log_dir, log_filename)
    
    logger.add(
        log_path,
        level=level,
        rotation="10 MB",  # 轮转大小
        retention="1 week", # 保留时间
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
        enqueue=True, # 异步写入
        backtrace=True,
        diagnose=True,
    )

    # 3. 拦截标准库 logging
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    
    # 配置特定的第三方库日志级别
    for _log in ["uvicorn", "uvicorn.error", "fastapi", "httpx", "httpcore"]:
        _logger = logging.getLogger(_log)
        _logger.handlers = [InterceptHandler()]
        _logger.propagate = False # 防止重复

    # 也可以设置 loguru 拦截所有 logging
    # logging.root.handlers = [InterceptHandler()]
    # logging.root.setLevel(level)

    logger.info("Loguru 日志系统初始化完成")

# 初始化默认 logger (可选，方便直接导入使用)
# setup_logger() 
