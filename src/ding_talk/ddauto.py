# 文件名: ddauto_win32.py
import win32gui
import win32api
import win32con
import win32clipboard
import pyperclip
import time
import os
import struct
import asyncio
from io import BytesIO
from curl_cffi import AsyncSession
from loguru import logger

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import uiautomation as auto
except ImportError:
    auto = None
    logger.warning("未安装 uiautomation 库，将仅使用 Win32 API 进行窗口激活")

from src.config import global_config
from src.utils.commons import timeit, extract_author
from src.utils.deduplication import mark_url_as_processed, mark_username_as_processed



class DingTalkNotFoundException(Exception):
    pass


class DingTalkAutomationException(Exception):
    pass


class DDAuto:
    def __init__(self, target_contact: str):
        """
        初始化钉钉窗口，切换目标联系人
        """
        logger.info(f"--- [DDAuto] 初始化开始，目标: '{target_contact}' ---")
        self.target_contact = target_contact

        self.hwnd = win32gui.FindWindow(None, "钉钉")
        if not self.hwnd:
            raise DingTalkNotFoundException("❌ 没有找到钉钉窗口，请确保已登录。")

        self._activate_window()
        
        # 检查配置，如果是 Standby 模式，则跳过搜索联系人步骤
        if global_config.DINGTALK_STANDBY == "1":
            logger.info("⚠️ 钉钉处于 Standby 模式，跳过联系人搜索初始化，直接进入发送就绪状态。")
        else:
            # 点击搜索框（窗口顶部中间，偏移约15px）
            self._click_search_box()
            # 输入联系人
            pyperclip.copy(target_contact)
            self._paste()
            time.sleep(1)
            self._press_enter()
            logger.info(f"✅ 钉钉已切换至联系人：{target_contact}")

        # 点击输入框（窗口底部中间偏上70px）
        self._click_input_box()
        
        # 初始化发送锁
        self.lock = asyncio.Lock()
        logger.info("--- [DDAuto] 初始化完成 ---")

    def _activate_window(self):
        """
        激活钉钉窗口，首选 UIA，兜底 Win32
        """
        success = False
        # 1. 尝试 UIA 方式
        if auto:
            try:
                window = auto.ControlFromHandle(self.hwnd)
                if window:
                    # SetFocus 会尝试将窗口前置并获取焦点
                    window.SetFocus()
                    success = True
                    # logger.debug("✅ [UIA] 窗口激活成功")
            except Exception as e:
                logger.warning(f"⚠️ [UIA] 窗口激活失败: {e}")
        
        # 2. 如果 UIA 失败或未安装，使用 Win32 兜底
        if not success:
            try:
                win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(self.hwnd)
                # logger.debug("✅ [Win32] 窗口激活成功")
            except Exception as e:
                logger.warning(f"⚠️ [Win32] 窗口激活失败: {e}")
        
        time.sleep(0.2)

    def _get_window_rect(self):
        left, top, right, bottom = win32gui.GetWindowRect(self.hwnd)
        width = right - left
        height = bottom - top
        return left, top, right, bottom, width, height

    def _click_search_box(self):
        left, top, _, _, width, _ = self._get_window_rect()
        x = left + width // 2
        y = top + 15  # 固定偏移
        logger.debug(f"🔍 搜索框点击坐标: ({x},{y})")
        self._click(x, y)
        time.sleep(1)

    def _click_input_box(self):
        left, _, _, bottom, width, _ = self._get_window_rect()
        x = left + width // 2
        y = bottom - 120
        logger.debug(f"✅ 输入框点击坐标: ({x},{y})")
        self._click(x, y)

    def _click(self, x, y):
        win32api.SetCursorPos((x, y))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)

    def _paste(self):
        win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
        win32api.keybd_event(ord('V'), 0, 0, 0)
        win32api.keybd_event(ord('V'), 0, win32con.KEYEVENTF_KEYUP, 0)
        win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)

    def _press_enter(self):
        win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
        win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)

    def _clean_input_box(self):
        """模拟 Ctrl+A 全选操作"""
        time.sleep(0.1)
        win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
        win32api.keybd_event(ord('A'), 0, 0, 0)
        win32api.keybd_event(ord('A'), 0, win32con.KEYEVENTF_KEYUP, 0)
        win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)
        win32api.keybd_event(win32con.VK_DELETE, 0, 0, 0)
        win32api.keybd_event(win32con.VK_DELETE, 0, win32con.KEYEVENTF_KEYUP, 0)
        logger.debug("✅ 输入框已清空")

    @timeit()
    def _set_clipboard_image(self, image_path: str):
        """
        将图片文件复制到剪贴板
        """
        if not Image:
            logger.error("PIL (Pillow) 库未安装，无法处理图片复制！")
            return False
            
        try:
            image = Image.open(image_path)
            output = BytesIO()
            image.convert("RGB").save(output, "BMP")
            data = output.getvalue()[14:] # 去掉 BMP 文件头 (14 bytes)，只保留 DIB 数据
            output.close()
            
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_DIB, data)
            win32clipboard.CloseClipboard()
            return True
        except Exception as e:
            logger.error(f"复制图片到剪贴板失败: {e}")
            try:
                win32clipboard.CloseClipboard()
            except:
                pass
            return False

    @timeit()
    def _set_clipboard_files(self, file_paths: list):
        """
        将文件列表复制到剪贴板
        """
        try:
            # 构造 DROPFILES 结构
            files = ("\0".join(file_paths) + "\0\0").encode('utf-16le')
            # DROPFILES structure: pFiles(4), pt(8), fNC(4), fWide(4)
            # pFiles = 20 (offset where files start)
            # fWide = 1 (Unicode)
            header = struct.pack('Iiiii', 20, 0, 0, 0, 1) 
            data = header + files
            
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_HDROP, data)
            win32clipboard.CloseClipboard()
            return True
        except Exception as e:
            logger.error(f"复制文件到剪贴板失败: {e}")
            try:
                win32clipboard.CloseClipboard()
            except:
                pass
            return False

    @timeit()
    async def send_video(self, video_path: str):
        """
        发送视频文件
        """
        if not video_path or not os.path.exists(video_path):
            logger.warning(f"视频文件不存在: {video_path}")
            return

        async with self.lock:
            logger.info(f"正在发送视频: {video_path}")
            try:
                # 激活窗口
                self._activate_window()
                self._click_input_box()
                self._clean_input_box()

                # 复制并粘贴视频
                if self._set_clipboard_files([video_path]):
                    self._paste()
                    # 视频可能较大，粘贴后需要等待一下让客户端识别
                    time.sleep(global_config.VIDEO_PASTE_WAITING) 
                    self._press_enter()
                    logger.info("✅ 视频发送指令已执行")
                else:
                    logger.error("❌ 视频复制到剪贴板失败")
            except Exception as e:
                logger.error(f"视频发送过程中出错: {e}")

    @timeit()
    async def send_mixed(self, msg: str, image_path: str = None):
        """
        发送混合消息：先发图片，再发文字（或者反过来，根据需求）
        """
        async with self.lock:
            logger.debug("准备向 '{}' 发送混合消息", self.target_contact)
            
            # 激活窗口
            self._activate_window()
            try:
                self._click_input_box()
                self._clean_input_box()
            except Exception as e:
                logger.warning("⚠️ 激活输入框失败: {}", e)

            
            # 2. 粘贴图片
            if image_path and os.path.exists(image_path):
                if self._set_clipboard_image(image_path):
                    logger.debug("图片已复制到剪贴板: {}", image_path)
                    # 粘贴图片
                    self._paste()
                    time.sleep(global_config.SCREENSHOT_PASTE_WAITING) # 等待图片上屏
                else:
                    logger.warning("图片复制失败，略过图片。")

            # 1. 粘贴文本
            if msg:
                # 预处理文本（加空格等）
                # msg = msg.replace("https://", "https:// ").replace("http://", "http:// ")
                pyperclip.copy(msg)
                self._paste()
                time.sleep(global_config.TEXT_PASTE_WAITING) # 等待文本上屏


            # 3. 统一发送
            self._press_enter()
            logger.info("✅ 混合消息发送指令已执行")

            # 4. 后续处理（标记URL等）
            if msg:
                match_obj = global_config.URL_PATTERN.search(msg)
                if match_obj:
                    url = match_obj.group(0)
                    await mark_url_as_processed(url)
                    username = extract_author(msg)
                    await mark_username_as_processed(username)
                    logger.debug("消息 媒体URL、作者 已缓存")
                
    @timeit()
    async def send(self, msg: str):
        async with self.lock:
            logger.debug("准备向 '{}' 发送消息: {}", self.target_contact, msg.replace('\n', ' ')[:100])
            # ✅ 每次发送前激活钉钉窗口
            self._activate_window()

            match_obj = global_config.URL_PATTERN.search(msg)
            msg = msg.replace("https://", "https:// ").replace("http://", "http:// ")

            # 粘贴并发送
            pyperclip.copy(msg)
            # 点击输入框
            self._click_input_box()
            # 全选
            self._clean_input_box()
            # 粘贴
            self._paste()
            # 等待文本上屏
            time.sleep(global_config.TEXT_PASTE_WAITING)
            # 回车发送
            self._press_enter()

            logger.info("✅ 钉钉消息发送成功")

            if match_obj:
                url = match_obj.group(0)
                await mark_url_as_processed(url)
                username = extract_author(msg)
                await mark_username_as_processed(username)
                logger.debug("消息 媒体URL、作者 已缓存")


if __name__ == '__main__':
    try:
        session = AsyncSession()
        dd_sender = DDAuto(target_contact="1111")
        dd_sender.send("你好，这是第一条消息。")
        time.sleep(1)
        dd_sender.send("你好，这是第二条消息。")
    except Exception as e:
        logger.error("自动化失败", exc_info=True)
