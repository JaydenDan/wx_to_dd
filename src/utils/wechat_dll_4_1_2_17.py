"""
WeChat hook helper for Loader_4.1.2.17 / Helper_4.1.2.17.
Copied and trimmed from the upstream demo to avoid HTTP exposure; only the
core DLL bridge + callbacks + send helpers are kept.
"""
import copy
import ctypes
import inspect
import json
import os
import sys
import time
from ctypes import WinDLL, WINFUNCTYPE, create_string_buffer
from functools import wraps
from typing import Optional

from loguru import logger


def is_64bit() -> bool:
    return sys.maxsize > 2 ** 32


def c_string(data: str):
    return ctypes.c_char_p(data.encode("utf-8"))


class MessageType:
    # 系统消息
    MT_DEBUG_LOG = 11024
    # 用户登陆消息类型
    MT_USER_LOGIN = 11025
    # 用户登出
    MT_USER_LOGOUT = 11026
    MT_USER_LOGOUT_2 = 11027
    #
    MT_SEND_TEXTMSG = 11036


class CallbackHandler:
    pass


class WeChatServiceHandler(CallbackHandler):
    """Base handler; override in the app."""

    pass


_GLOBAL_CONNECT_CALLBACK_LIST = []
_GLOBAL_RECV_CALLBACK_LIST = []
_GLOBAL_CLOSE_CALLBACK_LIST = []


def CONNECT_CALLBACK(in_class: bool = False):
    def decorator(func):
        wraps(func)
        if in_class:
            func._wx_connect_handled = True
        else:
            _GLOBAL_CONNECT_CALLBACK_LIST.append(func)
        return func

    return decorator


def RECV_CALLBACK(in_class: bool = False):
    def decorator(func):
        wraps(func)
        if in_class:
            func._wx_recv_handled = True
        else:
            _GLOBAL_RECV_CALLBACK_LIST.append(func)
        return func

    return decorator


def CLOSE_CALLBACK(in_class: bool = False):
    def decorator(func):
        wraps(func)
        if in_class:
            func._wx_close_handled = True
        else:
            _GLOBAL_CLOSE_CALLBACK_LIST.append(func)
        return func

    return decorator


def add_callback_handler(callback_handler: CallbackHandler):
    for _, handler in inspect.getmembers(callback_handler, callable):
        if hasattr(handler, "_wx_connect_handled"):
            _GLOBAL_CONNECT_CALLBACK_LIST.append(handler)
        elif hasattr(handler, "_wx_recv_handled"):
            _GLOBAL_RECV_CALLBACK_LIST.append(handler)
        elif hasattr(handler, "_wx_close_handled"):
            _GLOBAL_CLOSE_CALLBACK_LIST.append(handler)


@WINFUNCTYPE(None, ctypes.c_void_p)
def wechat_connect_callback(client_id):
    for func in _GLOBAL_CONNECT_CALLBACK_LIST:
        func(client_id)


@WINFUNCTYPE(None, ctypes.c_long, ctypes.c_char_p, ctypes.c_ulong)
def wechat_recv_callback(client_id, data, length):
    # data is a bytes string containing JSON
    data = copy.deepcopy(data)
    json_data = data.decode("utf-8")
    dict_data = json.loads(json_data)
    for func in _GLOBAL_RECV_CALLBACK_LIST:
        func(client_id, dict_data["type"], dict_data["data"])


@WINFUNCTYPE(None, ctypes.c_ulong)
def wechat_close_callback(client_id):
    for func in _GLOBAL_CLOSE_CALLBACK_LIST:
        func(client_id)


class WeChatServiceHandler(CallbackHandler):
    """Default logging-only handler."""

    def __init__(self, service: "WeChatService"):
        self.service = service
        self.connected_clients = set()

    @CONNECT_CALLBACK(in_class=True)
    def on_connect(self, client_id):
        self.connected_clients.add(client_id)
        logger.info("客户端 {} 已连接，当前连接数：{}", client_id, len(self.connected_clients))

    @RECV_CALLBACK(in_class=True)
    def on_receive(self, client_id, message_type, data):
        logger.info("收到消息：type={} data={}", message_type, data)

    @CLOSE_CALLBACK(in_class=True)
    def on_close(self, client_id):
        self.connected_clients.discard(client_id)
        logger.info("客户端 {} 已断开，当前连接数：{}", client_id, len(self.connected_clients))


class NoveLoader:
    # offsets
    _InitWeChatSocket: int = 0xB080
    _GetUserWeChatVersion: int = 0xCB80
    _InjectWeChat: int = 0xCC10
    _SendWeChatData: int = 0xAF90
    _DestroyWeChat: int = 0xC540
    _UseUtf8: int = 0xC680
    _InjectWeChat2: int = 0x14D7
    _InjectWeChatPid: int = 0xB750
    _InjectWeChatMultiOpen: int = 0xC780

    def __init__(self, loader_path: str):
        loader_path = os.path.realpath(loader_path)
        if not os.path.exists(loader_path):
            raise FileNotFoundError(f"Loader DLL 未找到: {loader_path}")

        loader_module = WinDLL(loader_path)
        self.loader_module_base = loader_module._handle

        self.create_shared_memory()
        self.UseUtf8()
        self.InitWeChatSocket(wechat_connect_callback, wechat_recv_callback, wechat_close_callback)

    def __get_non_exported_func(self, offset: int, arg_types, return_type):
        func_addr = self.loader_module_base + offset
        if arg_types:
            func_type = ctypes.WINFUNCTYPE(return_type, *arg_types)
        else:
            func_type = ctypes.WINFUNCTYPE(return_type)
        return func_type(func_addr)

    def create_shared_memory(self) -> bool:
        try:
            kernel32 = ctypes.windll.kernel32
            file_mapping = kernel32.CreateFileMappingA(-1, None, 0x04, 0, 33, b"windows_shell_global__")
            if file_mapping == 0:
                logger.error("CreateFileMappingA 失败，错误码={}", kernel32.GetLastError())
                return False
            mapped_address = kernel32.MapViewOfFile(file_mapping, 0x000F001F, 0, 0, 0)
            if mapped_address == 0:
                logger.error("MapViewOfFile 失败，错误码={}", kernel32.GetLastError())
                kernel32.CloseHandle(file_mapping)
                return False
            key = b"3101b223dca7715b0154924f0eeeee20"
            ctypes.memmove(mapped_address, ctypes.byref(ctypes.create_string_buffer(key)), len(key))
            return True
        except Exception as exc:  # pragma: no cover - Windows only
            logger.error("创建共享内存异常: {}", exc)
            return False

    def add_callback_handler(self, callback_handler: CallbackHandler):
        add_callback_handler(callback_handler)

    def InitWeChatSocket(self, connect_callback, recv_callback, close_callback):
        func = self.__get_non_exported_func(
            self._InitWeChatSocket,
            [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p],
            ctypes.c_bool,
        )
        return func(connect_callback, recv_callback, close_callback)

    def GetUserWeChatVersion(self) -> str:
        func = self.__get_non_exported_func(self._GetUserWeChatVersion, None, ctypes.c_bool)
        out = create_string_buffer(20)
        if func(out):
            return out.value.decode("utf-8")
        return ""

    def InjectWeChat(self, dll_path: str) -> ctypes.c_uint32:
        func = self.__get_non_exported_func(self._InjectWeChat, [ctypes.c_char_p], ctypes.c_uint32)
        return func(c_string(dll_path))

    def SendWeChatData(self, client_id: int, message: str) -> ctypes.c_bool:
        func = self.__get_non_exported_func(self._SendWeChatData, [ctypes.c_uint32, ctypes.c_char_p], ctypes.c_bool)
        return func(client_id, c_string(message))

    def DestroyWeChat(self) -> ctypes.c_bool:
        func = self.__get_non_exported_func(self._DestroyWeChat, None, ctypes.c_bool)
        return func()

    def UseUtf8(self):
        func = self.__get_non_exported_func(self._UseUtf8, None, ctypes.c_bool)
        return func()

    def InjectWeChatPid(self, pid: int, dll_path: str) -> ctypes.c_uint32:
        func = self.__get_non_exported_func(self._InjectWeChatPid, [ctypes.c_uint32, ctypes.c_char_p], ctypes.c_uint32)
        return func(pid, c_string(dll_path))

    def InjectWeChatMultiOpen(self, dll_path: str, exe_path: str) -> ctypes.c_uint32:
        func = self.__get_non_exported_func(
            self._InjectWeChatMultiOpen,
            [ctypes.c_char_p, ctypes.c_char_p],
            ctypes.c_uint32,
        )
        return func(c_string(dll_path), c_string(exe_path))


class WeChatService:
    """Minimal service wrapper, no Flask."""

    def __init__(self, loader_path: str, dll_path: str):
        self.loader_path = loader_path
        self.dll_path = dll_path
        self.loader: Optional[NoveLoader] = None
        self.handler: Optional[CallbackHandler] = None
        self.is_running = False
        self.should_stop = False
        self.client_id: Optional[int] = None
        self.last_heartbeat = time.time()
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
        self.reconnect_delay = 10

    def set_handler(self, handler: CallbackHandler):
        self.handler = handler

    def initialize(self) -> bool:
        try:
            if is_64bit():
                logger.error("检测到 64 位 Python，需要 32 位 Python 才能加载 DLL。")
                return False
            if not os.path.exists(self.loader_path) or not os.path.exists(self.dll_path):
                logger.error("DLL 未找到: loader={} helper={}", self.loader_path, self.dll_path)
                return False

            self.loader = NoveLoader(self.loader_path)
            if not self.loader:
                logger.error("初始化 loader 失败")
                return False

            if not self.handler:
                self.handler = WeChatServiceHandler(self)
            self.loader.add_callback_handler(self.handler)
            logger.info("微信服务初始化完成")
            return True
        except Exception as exc:
            logger.error("初始化异常: {}", exc)
            return False

    def start(self) -> bool:
        if not self.initialize():
            return False
        self.is_running = True
        self.should_stop = False
        try:
            logger.info("开始注入微信...")
            self.client_id = self.loader.InjectWeChat(self.dll_path) if self.loader else None
            if self.client_id:
                logger.info("注入成功，client id={}", self.client_id)
                self.reconnect_attempts = 0
                self.run_service()
                return True
            logger.error("注入微信失败")
            return False
        except Exception as exc:
            logger.error("启动失败: {}", exc)
            return False

    def run_service(self):
        try:
            while self.is_running and not self.should_stop:
                time.sleep(1)
        except Exception as exc:
            logger.error("运行异常: {}", exc)
        finally:
            self.stop()

    def reconnect(self) -> bool:
        if self.reconnect_attempts >= self.max_reconnect_attempts:
            logger.error("重连次数已达上限")
            return False
        self.reconnect_attempts += 1
        logger.info("正在重连，第 {} 次", self.reconnect_attempts)
        try:
            if self.loader:
                self.loader.DestroyWeChat()
            time.sleep(self.reconnect_delay)
            self.client_id = self.loader.InjectWeChat(self.dll_path) if self.loader else None
            if self.client_id:
                logger.info("重连成功，client id={}", self.client_id)
                self.last_heartbeat = time.time()
                self.reconnect_attempts = 0
                return True
            logger.error("重连失败")
            return False
        except Exception as exc:
            logger.error("重连异常: {}", exc)
            return False

    def stop(self):
        logger.info("正在停止微信服务...")
        self.should_stop = True
        self.is_running = False
        try:
            if self.loader:
                self.loader.DestroyWeChat()
                logger.info("微信连接已销毁")
        except Exception as exc:
            logger.error("停止时异常: {}", exc)

    def send_message(self, message: str) -> bool:
        if not self.client_id or not self.loader:
            logger.error("未连接服务，无法发送消息")
            return False
        try:
            ok = self.loader.SendWeChatData(self.client_id, message)
            if ok:
                logger.info("发送成功: {}", message)
                return True
            logger.error("发送失败: {}", message)
            return False
        except Exception as exc:
            logger.error("发送异常: {}", exc)
            return False

    def send_startup_payload(self, room_wxid: str, status: int = 0) -> bool:
        payload = {"data": {"room_wxid": room_wxid, "status": status}, "type": 11075}
        message = json.dumps(payload, ensure_ascii=False)
        return self.send_message(message)
