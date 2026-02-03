# -*- coding: utf8 -*-

import json
import sys
import os
import os.path
import inspect
import copy
import ctypes
import signal
import threading
import time
from datetime import datetime
from flask import Flask, request, jsonify
from functools import wraps
from ctypes import WinDLL, create_string_buffer, WINFUNCTYPE

from loguru import logger

# 配置日志
logger.remove()
logger.add(sys.stdout, level="INFO", format='{time:YYYY-MM-DD HH:mm:ss.SSS} - {name} - {level} - {message}')
logger.add('wechat_service.log', level="INFO", encoding='utf-8', format='{time:YYYY-MM-DD HH:mm:ss.SSS} - {name} - {level} - {message}')

def is_64bit():
    return sys.maxsize > 2 ** 32


def c_string(data):
    return ctypes.c_char_p(data.encode('utf-8'))


class MessageType:
    MT_DEBUG_LOG = 11024
    MT_USER_LOGIN = 11025
    MT_USER_LOGOUT = 11026
    MT_USER_LOGOUT = 11027
    MT_SEND_TEXTMSG = 11036


class CallbackHandler:
    pass


class WeChatServiceHandler(CallbackHandler):
    """微信服务回调处理器占位，稍后重新定义在装饰器之后"""
    pass


_GLOBAL_CONNECT_CALLBACK_LIST = []
_GLOBAL_RECV_CALLBACK_LIST = []
_GLOBAL_CLOSE_CALLBACK_LIST = []


def CONNECT_CALLBACK(in_class=False):
    def decorator(f):
        wraps(f)
        if in_class:
            f._wx_connect_handled = True
        else:
            _GLOBAL_CONNECT_CALLBACK_LIST.append(f)
        return f

    return decorator


def RECV_CALLBACK(in_class=False):
    def decorator(f):
        wraps(f)
        if in_class:
            f._wx_recv_handled = True
        else:
            _GLOBAL_RECV_CALLBACK_LIST.append(f)
        return f

    return decorator


def CLOSE_CALLBACK(in_class=False):
    def decorator(f):
        wraps(f)
        if in_class:
            f._wx_close_handled = True
        else:
            _GLOBAL_CLOSE_CALLBACK_LIST.append(f)
        return f

    return decorator


def add_callback_handler(callbackHandler):
    for dummy, handler in inspect.getmembers(callbackHandler, callable):
        if hasattr(handler, '_wx_connect_handled'):
            _GLOBAL_CONNECT_CALLBACK_LIST.append(handler)
        elif hasattr(handler, '_wx_recv_handled'):
            _GLOBAL_RECV_CALLBACK_LIST.append(handler)
        elif hasattr(handler, '_wx_close_handled'):
            _GLOBAL_CLOSE_CALLBACK_LIST.append(handler)


@WINFUNCTYPE(None, ctypes.c_void_p)
def wechat_connect_callback(client_id):
    for func in _GLOBAL_CONNECT_CALLBACK_LIST:
        func(client_id)


@WINFUNCTYPE(None, ctypes.c_long, ctypes.c_char_p, ctypes.c_ulong)
def wechat_recv_callback(client_id, data, length):
    data = copy.deepcopy(data)
    json_data = data.decode('utf-8')
    dict_data = json.loads(json_data)
    for func in _GLOBAL_RECV_CALLBACK_LIST:
        func(client_id, dict_data['type'], dict_data['data'])


@WINFUNCTYPE(None, ctypes.c_ulong)
def wechat_close_callback(client_id):
    for func in _GLOBAL_CLOSE_CALLBACK_LIST:
        func(client_id)


class WeChatServiceHandler(CallbackHandler):
    """微信服务回调处理器"""
    
    def __init__(self, service):
        self.service = service
        self.connected_clients = set()
    
    @CONNECT_CALLBACK(in_class=True)
    def on_connect(self, client_id):
        """客户端连接回调"""
        self.connected_clients.add(client_id)
        logger.info(f"客户端 {client_id} 已连接，当前连接数: {len(self.connected_clients)}")
        
    @RECV_CALLBACK(in_class=True)
    def on_receive(self, client_id, message_type, data):
        """接收消息回调"""
        logger.info(f"收到来自客户端 {client_id} 的消息 - 类型: {message_type}, 数据: {data}")
        
        # 处理不同类型的消息
        if message_type == MessageType.MT_USER_LOGIN:
            logger.info(f"用户登录: {data}")
            # 登录成功后发送一次业务消息（固定 ROOM_WXID）
            try:
                self.service.send_startup_payload(room_wxid="47945916190@chatroom", status=0)
            except Exception as e:
                logger.error(f"登录回调发送启动消息失败: {e}")

        elif message_type == MessageType.MT_USER_LOGOUT:
            logger.info(f"用户登出: {data}")
        elif message_type == MessageType.MT_DEBUG_LOG:
            logger.debug(f"调试日志: {data}")
            
    @CLOSE_CALLBACK(in_class=True)
    def on_close(self, client_id):
        """客户端断开回调"""
        self.connected_clients.discard(client_id)
        logger.info(f"客户端 {client_id} 已断开，当前连接数: {len(self.connected_clients)}")


class NoveLoader:
    # 加载器
    loader_module_base: int = 0

    # 偏移
    _InitWeChatSocket: int = 0xB080
    _GetUserWeChatVersion: int = 0xCB80
    _InjectWeChat: int = 0xCC10
    _SendWeChatData: int = 0xAF90
    _DestroyWeChat: int = 0xC540
    _UseUtf8: int = 0xC680
    _InjectWeChat2: int = 0x14D7
    _InjectWeChatPid: int = 0xB750
    _InjectWeChatMultiOpen: int = 0xC780

    # _GetInstallWeixinVersion: int = 0x0
    # _InjectWeixin: int = 0x0
    # _InjectWeixin2: int = 0x0
    # _SetWeixinDataLocationPath: int = 0x0
    # _GetWeixinDataLocationPath: int = 0x0

    def create_shared_memory(self):
        """创建共享内存并写入密钥"""
        try:
            # 导入Windows API
            kernel32 = ctypes.windll.kernel32
            
            # 创建文件映射对象
            # 参数: hFile=-1(使用页面文件), lpSecurityAttributes=None, flProtect=PAGE_READWRITE,
            # dwMaximumSizeHigh=0, dwMaximumSizeLow=33, lpName="windows_shell_global__"
            file_mapping = kernel32.CreateFileMappingA(
                -1,  # INVALID_HANDLE_VALUE，表示使用页面文件
                None,
                0x04,  # PAGE_READWRITE
                0,
                33,    # 大小为33字节
                "windows_shell_global__"
            )
            
            if file_mapping == 0:
                logger.error(f"创建映射文件失败，错误码: {kernel32.GetLastError()}")
                return False
            
            # 映射视图到内存
            # 参数: hFileMappingObject=file_mapping, dwDesiredAccess=FILE_MAP_ALL_ACCESS,
            # dwFileOffsetHigh=0, dwFileOffsetLow=0, dwNumberOfBytesToMap=0(映射全部)
            mapped_address = kernel32.MapViewOfFile(
                file_mapping,
                0x000F001F,  # FILE_MAP_ALL_ACCESS
                0,
                0,
                0
            )
            
            if mapped_address == 0:
                logger.error(f"映射到内存失败，错误码: {kernel32.GetLastError()}")
                kernel32.CloseHandle(file_mapping)
                return False
            
            # 准备密钥数据
            key = "3101b223dca7715b0154924f0eeeee20".encode('utf-8')
            
            # 将密钥复制到共享内存
            ctypes.memmove(mapped_address, ctypes.byref(ctypes.create_string_buffer(key)), len(key))
            
            # 注意：这里没有关闭句柄，因为共享内存可能需要在程序运行期间保持打开
            # 如果需要关闭，可以调用以下代码：
            # kernel32.UnmapViewOfFile(mapped_address)
            # kernel32.CloseHandle(file_mapping)
            
            logger.info("共享内存创建成功并写入密钥")
            return True
            
        except Exception as e:
            logger.error(f"创建共享内存时出错: {e}")
            return False

    def __init__(self, loader_path: str):
        loader_path = os.path.realpath(loader_path)
        if not os.path.exists(loader_path):
            logger.error('libs path error or loader not exist')
            return

        loader_module = WinDLL(loader_path)
        self.loader_module_base = loader_module._handle

        # 创建共享内存并写入密钥
        self.create_shared_memory()
        
        # 使用utf8编码
        self.UseUtf8()

        # 初始化接口回调
        self.InitWeChatSocket(wechat_connect_callback, wechat_recv_callback, wechat_close_callback)

    def __get_non_exported_func(self, offset: int, arg_types, return_type):
        func_addr = self.loader_module_base + offset
        if arg_types:
            func_type = ctypes.WINFUNCTYPE(return_type, *arg_types)
        else:
            func_type = ctypes.WINFUNCTYPE(return_type)
        return func_type(func_addr)

    def add_callback_handler(self, callback_handler):
        add_callback_handler(callback_handler)

    def InitWeChatSocket(self, connect_callback, recv_callback, close_callback):
        func = self.__get_non_exported_func(self._InitWeChatSocket, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_bool)
        return func(connect_callback, recv_callback, close_callback)

    def GetUserWeChatVersion(self) -> str:
        func = self.__get_non_exported_func(self._GetUserWeChatVersion, None, ctypes.c_bool)
        out = create_string_buffer(20)
        if func(out):
            return out.value.decode('utf-8')
        else:
            return ''

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

    def InjectWeChat2(self, dll_path: str, exe_path: str) -> ctypes.c_uint32:
        func = self.__get_non_exported_func(self._InjectWeChat2, [ctypes.c_char_p, ctypes.c_char_p], ctypes.c_uint32)
        return func(c_string(dll_path), c_string(exe_path))

    def InjectWeChatPid(self, pid: int, dll_path: str) -> ctypes.c_uint32:
        func = self.__get_non_exported_func(self._InjectWeChatPid, [ctypes.c_uint32, ctypes.c_char_p], ctypes.c_uint32)
        return func(pid, c_string(dll_path))

    def InjectWeChatMultiOpen(self, dll_path: str, exe_path: str) -> ctypes.c_uint32:
        func = self.__get_non_exported_func(self._InjectWeChatMultiOpen, [ctypes.c_char_p, ctypes.c_char_p], ctypes.c_uint32)
        return func(c_string(dll_path), c_string(exe_path))

    def GetInstallWeixinVersion(self) -> str:
        func = self.__get_non_exported_func(self._GetInstallWeixinVersion, None, ctypes.c_bool)
        out = create_string_buffer(20)
        if func(out):
            return out.value.decode('utf-8')
        else:
            return ''


class WeChatService:
    """微信服务管理器"""
    
    def __init__(self, loader_path: str, dll_path: str):
        self.loader_path = loader_path
        self.dll_path = dll_path
        self.loader = None
        self.handler = None
        self.is_running = False
        self.should_stop = False
        self.client_id = None
        self.heartbeat_thread = None
        self.last_heartbeat = time.time()
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
        self.reconnect_delay = 10  # 秒
        
    def initialize(self):
        """初始化服务"""
        try:
            logger.info("正在初始化微信服务...")
            
            # 检查Python架构
            if is_64bit():
                logger.error("检测到64位Python，但DLL是32位的。请使用32位Python运行此程序。")
                return False
            
            # 检查文件是否存在
            if not os.path.exists(self.loader_path):
                logger.error(f"Loader DLL 文件不存在: {self.loader_path}")
                return False
                
            if not os.path.exists(self.dll_path):
                logger.error(f"Helper DLL 文件不存在: {self.dll_path}")
                return False
            
            # 创建加载器
            self.loader = NoveLoader(self.loader_path)
            if not self.loader:
                logger.error("创建 NoveLoader 失败")
                return False
            
            # 创建回调处理器
            self.handler = WeChatServiceHandler(self)
            self.loader.add_callback_handler(self.handler)
            
            logger.info("微信服务初始化成功")
            return True
            
        except Exception as e:
            logger.error(f"初始化微信服务失败: {e}")
            return False
    
    def start(self):
        """启动服务"""
        if not self.initialize():
            return False
            
        self.is_running = True
        self.should_stop = False
        
        try:
            # 注入微信
            logger.info("正在注入微信...")
            self.client_id = self.loader.InjectWeChat(self.dll_path)
            
            if self.client_id:
                logger.info(f"成功注入微信，客户端 ID 为: {self.client_id}")
                self.reconnect_attempts = 0
                

                # 启动心跳监控
                self.start_heartbeat()
                
                # 启动主服务循环
                self.run_service()
                return True
            else:
                logger.error("注入微信失败")
                return False
                
        except Exception as e:
            logger.error(f"启动微信服务失败: {e}")
            return False
    
    def start_heartbeat(self):
        """启动心跳监控线程"""
        logger.info("心跳监控已启动")
    


    def run_service(self):
        """运行服务主循环"""
        logger.info("微信服务已启动，正在运行...")
        
        try:
            while self.is_running and not self.should_stop:
                # 检查是否需要重连
                # if time.time() - self.last_heartbeat > 120:  # 2分钟无心跳
                #     logger.warning("检测到连接超时，尝试重连...")
                #     if self.reconnect():
                #         continue
                #     else:
                #         break
                
                # time.sleep()
                pass
                
        except KeyboardInterrupt:
            logger.info("收到中断信号，正在停止服务...")
            self.should_stop = True
        except Exception as e:
            logger.error(f"服务运行异常: {e}")
        finally:
            self.stop()
    
    def reconnect(self):
        """重连服务"""
        if self.reconnect_attempts >= self.max_reconnect_attempts:
            logger.error(f"重连次数超过限制 ({self.max_reconnect_attempts})，停止重连")
            return False
        
        self.reconnect_attempts += 1
        logger.info(f"尝试重连 ({self.reconnect_attempts}/{self.max_reconnect_attempts})...")
        
        try:
            # 清理当前连接
            if self.loader:
                self.loader.DestroyWeChat()
            
            time.sleep(self.reconnect_delay)
            
            # 重新注入
            self.client_id = self.loader.InjectWeChat(self.dll_path)
            if self.client_id:
                logger.info(f"重连成功，客户端 ID: {self.client_id}")
                self.last_heartbeat = time.time()
                self.reconnect_attempts = 0
                return True
            else:
                logger.error("重连失败")
                return False
                
        except Exception as e:
            logger.error(f"重连过程中发生异常: {e}")
            return False
    
    def stop(self):
        """停止服务"""
        logger.info("正在停止微信服务...")
        self.should_stop = True
        self.is_running = False
        
        try:
            if self.loader:
                self.loader.DestroyWeChat()
                logger.info("微信连接已断开")
        except Exception as e:
            logger.error(f"停止服务时发生异常: {e}")
        
        logger.info("微信服务已停止")
    
    def send_message(self, message: str):
        """发送消息"""
        if not self.client_id or not self.loader:
            logger.error("服务未连接，无法发送消息")
            return False
        
        try:
            result = self.loader.SendWeChatData(1, message)
            if result:
                logger.info(f"消息发送成功: {message}")
                return True
            else:
                logger.error(f"消息发送失败: {result}")
                return False
        except Exception as e:
            logger.error(f"发送消息时发生异常: {e}")
            return False

    def send_startup_payload(self, room_wxid: str, status: int = 0) -> bool:
        """按需求在启动时发送一次固定结构的消息"""
        payload = {
            "data": {
                "room_wxid": room_wxid,
                "status": status
            },
            "type": 11075
        }
        message = json.dumps(payload, ensure_ascii=False)
        logger.info(f"启动消息发送: {message}")
        return self.send_message(message)


# --- SIMPLE HTTP API ---
app = Flask(__name__)


@app.route('/send', methods=['POST'])
def api_send():
    """HTTP API: 接收文本或自定义 payload，转为 JSON 后调 send_message"""
    try:
        body = request.get_json(silent=True) or {}

        # 优先支持自定义 payload: { "type": ..., "data": {...} }
        custom_type = body.get('type')
        custom_data = body.get('data')

        if custom_type is not None and isinstance(custom_data, dict):
            payload = {
                'type': custom_type,
                'data': custom_data
            }
        else:
            # 文本直发: { "text": "...", "room_wxid": "..." }
            text = body.get('text') or body.get('message')
            room_wxid = body.get('room_wxid') or "47945916190@chatroom"
            if not text:
                return jsonify({'success': False, 'error': 'text is required'}), 400

            payload = {
                'type': MessageType.MT_SEND_TEXTMSG,
                'data': {
                    'room_wxid': room_wxid,
                    'content': text
                }
            }

        message = json.dumps(payload, ensure_ascii=False)
        ok = service.send_message(message)
        return jsonify({'success': bool(ok), 'payload': payload}), (200 if ok else 500)

    except Exception as e:
        logger.error(f"/send 接口异常: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# --- MAIN EXECUTION LOGIC ---
if __name__ == '__main__':
    # 配置 DLL 路径（可改为环境变量或配置文件）
    loader_path = "./Loader_4.1.2.17.dll"
    dll_path = "./Helper_4.1.2.17.dll"

    service = WeChatService(loader_path, dll_path)

    # 安装信号处理器，支持 Ctrl+C (SIGINT) 与 Windows 控制台中断 (SIGBREAK)
    def _signal_handler(signum, frame):
        logger.info(f"收到信号 {signum}，准备停止服务...")
        service.should_stop = True

    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, 'SIGTERM'):
        try:
            signal.signal(signal.SIGTERM, _signal_handler)
        except Exception:
            pass
    if hasattr(signal, 'SIGBREAK'):
        try:
            signal.signal(signal.SIGBREAK, _signal_handler)
        except Exception:
            pass

    # 启动服务（后台线程中运行主循环）
    t = threading.Thread(target=service.start, daemon=True)
    t.start()

    # 启动 HTTP API 服务
    api_host = os.environ.get('API_HOST', '0.0.0.0')
    api_port = int(os.environ.get('API_PORT', '5000'))
    logger.info(f"HTTP API 启动于 http://{api_host}:{api_port}")
    try:
        app.run(host=api_host, port=api_port, debug=False, use_reloader=False)
    finally:
        service.should_stop = True