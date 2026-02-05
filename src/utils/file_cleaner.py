import os
import time
import threading
from loguru import logger
from src.config import global_config

class FileCleaner:
    def __init__(self, interval_seconds=60, max_age_seconds=300):
        """
        初始化文件清理器
        :param interval_seconds: 检查间隔（秒）
        :param max_age_seconds: 文件最大存活时间（秒），默认5分钟
        """
        self.interval_seconds = interval_seconds
        self.max_age_seconds = max_age_seconds
        self.running = False
        self.thread = None
        
        # 定义需要清理的根目录
        # 1. 截图目录 (项目根目录下的 screenshots)
        self.screenshot_dir = os.path.join(os.getcwd(), "screenshots")
        # 2. 视频下载目录 (项目根目录下的 videodl_outputs)
        self.video_dir = os.path.join(os.getcwd(), "videodl_outputs")

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="FileCleanerThread")
        self.thread.start()
        logger.info(f"FileCleaner 已启动，每 {self.interval_seconds} 秒清理一次 {self.max_age_seconds} 秒前的文件。")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
            logger.info("FileCleaner 已停止。")

    def _run_loop(self):
        while self.running:
            try:
                self._clean_directory(self.screenshot_dir, [".png", ".jpg", ".jpeg"])
                self._clean_directory(self.video_dir, [".mp4", ".mov", ".avi"])
            except Exception as e:
                logger.error(f"FileCleaner 运行出错: {e}")
            
            # 简单的休眠循环
            for _ in range(self.interval_seconds):
                if not self.running:
                    break
                time.sleep(1)

    def _clean_directory(self, dir_path, extensions):
        if not os.path.exists(dir_path):
            return

        current_time = time.time()
        
        # 遍历目录 (包括子目录)
        for root, dirs, files in os.walk(dir_path):
            for file in files:
                if not self.running:
                    return
                    
                if any(file.lower().endswith(ext) for ext in extensions):
                    file_path = os.path.join(root, file)
                    try:
                        file_mtime = os.path.getmtime(file_path)
                        age = current_time - file_mtime
                        
                        if age > self.max_age_seconds:
                            try:
                                os.remove(file_path)
                                logger.info(f"🗑️ [自动清理] 已删除过期文件 ({int(age)}s): {file_path}")
                            except PermissionError:
                                logger.warning(f"无法删除文件 (被占用): {file_path}")
                            except Exception as e:
                                logger.warning(f"删除文件失败 {file_path}: {e}")
                    except FileNotFoundError:
                        pass
                    except Exception as e:
                        logger.warning(f"检查文件出错 {file_path}: {e}")
