# 微信Hook Python 演示项目

## 项目简介

这是一个基于Python开发的微信Hook演示项目，通过调用Windows API实现与微信客户端的交互。该项目提供了一套完整的微信消息监听、接收和发送功能，同时内置了简单的HTTP API接口，方便开发者集成到自己的应用中。

## 功能特点

- **微信进程注入**: 支持多种注入方式，包括常规注入、按PID注入和多开注入
- **消息监听与回调**: 自动监听用户登录、登出等事件，并触发相应回调函数
- **共享内存通信**: 使用Windows共享内存机制创建通信通道
- **心跳监控**: 内置心跳检测和自动重连机制
- **HTTP API接口**: 提供简单的HTTP接口发送微信消息
- **多客户端支持**: 支持同时处理多个客户端连接
- **完整日志记录**: 详细记录所有操作日志，便于调试和问题排查

## 项目结构

```
├── pythondemo.py       # 主程序文件
├── Loader_4.1.2.17.dll # 加载器DLL
├── Helper_4.1.2.17.dll # 辅助功能DLL
└── wechat_service.log  # 日志文件（运行时生成）
```

## 核心组件

### 1. MessageType

定义了微信消息的各种类型常量，包括：
- `MT_DEBUG_LOG`: 调试日志消息
- `MT_USER_LOGIN`: 用户登录消息
- `MT_USER_LOGOUT`: 用户登出消息
- `MT_SEND_TEXTMSG`: 发送文本消息

### 2. NoveLoader

负责加载和调用DLL中的非导出函数，主要功能包括：
- 创建共享内存并写入密钥
- 初始化微信Socket连接
- 获取微信版本信息
- 注入微信进程
- 发送微信数据
- 销毁微信连接

### 3. WeChatServiceHandler

微信服务回调处理器，处理各类事件回调：
- `on_connect`: 客户端连接时触发
- `on_receive`: 收到消息时触发
- `on_close`: 客户端断开时触发

### 4. WeChatService

微信服务管理器，提供完整的服务生命周期管理：
- `initialize`: 初始化服务
- `start`: 启动服务
- `stop`: 停止服务
- `reconnect`: 重连服务
- `send_message`: 发送消息
- 心跳监控

### 5. HTTP API

提供简单的HTTP接口，支持：
- 发送自定义payload消息
- 发送文本消息

## 技术要点

### Windows共享内存实现

项目使用Windows API创建命名共享内存，并写入特定密钥，实现进程间通信：

```python
def create_shared_memory(self):
    # 导入Windows API
    kernel32 = ctypes.windll.kernel32
    
    # 创建文件映射对象
    file_mapping = kernel32.CreateFileMappingA(
        -1,  # INVALID_HANDLE_VALUE
        None,
        0x04,  # PAGE_READWRITE
        0,
        33,    # 大小为33字节
        "windows_shell_global__"
    )
    
    # 映射视图到内存
    mapped_address = kernel32.MapViewOfFile(
        file_mapping,
        0x000F001F,  # FILE_MAP_ALL_ACCESS
        0,
        0,
        0
    )
    
    # 写入密钥数据
    key = "3101b223dca7715b0154924f0eeeee20".encode('utf-8')
    ctypes.memmove(mapped_address, ctypes.byref(ctypes.create_string_buffer(key)), len(key))
```

### 动态函数调用

通过计算函数偏移地址，动态调用DLL中的非导出函数：

```python
def __get_non_exported_func(self, offset: int, arg_types, return_type):
    func_addr = self.loader_module_base + offset
    if arg_types:
        func_type = ctypes.WINFUNCTYPE(return_type, *arg_types)
    else:
        func_type = ctypes.WINFUNCTYPE(return_type)
    return func_type(func_addr)
```

### 回调机制

使用装饰器实现灵活的事件回调注册：

```python
@CONNECT_CALLBACK(in_class=True)
def on_connect(self, client_id):
    # 处理客户端连接
    pass

@RECV_CALLBACK(in_class=True)
def on_receive(self, client_id, message_type, data):
    # 处理接收到的消息
    pass
```

## 环境要求

1. **Python 3.x** (必须使用32位Python，因为DLL是32位的)
2. **Windows操作系统** (仅支持Windows)
3. **依赖库**:
   - flask
   - ctypes (Python标准库)

## 使用方法

### 1. 准备工作

- 确保已安装32位Python
- 确保`Loader_4.1.2.17.dll`和`Helper_4.1.2.17.dll`文件在程序同目录下
- 安装依赖: `pip install flask`

### 2. 运行程序

```bash
python pythondemo.py
```

程序启动后，将：
- 初始化微信服务
- 注入微信进程
- 启动心跳监控
- 启动HTTP API服务（默认监听在 http://0.0.0.0:5000）

### 3. 使用HTTP API

**发送文本消息**:

```bash
curl -X POST http://localhost:5000/send \
  -H "Content-Type: application/json" \
  -d '{"text": "你好，这是一条测试消息", "room_wxid": "xxx@chatroom"}'
```

**发送自定义消息**:

```bash
curl -X POST http://localhost:5000/send \
  -H "Content-Type: application/json" \
  -d '{"type": 11075, "data": {"room_wxid": "xxx@chatroom", "status": 0}}'
```

## 配置选项

### 环境变量

- `API_HOST`: HTTP API监听地址，默认为`0.0.0.0`
- `API_PORT`: HTTP API监听端口，默认为`5000`

### 代码配置

- `max_reconnect_attempts`: 最大重连次数，默认为5
- `reconnect_delay`: 重连延迟时间（秒），默认为10
- `heartbeat_timeout`: 心跳超时时间（秒），默认为120

## 注意事项

1. **兼容性**
   - 该项目仅支持Windows平台
   - 必须使用32位Python运行
   - DLL版本为4.1.2.17，可能仅兼容特定版本的微信客户端

2. **安全性**
   - 项目使用共享内存机制，请注意内存安全
   - 共享内存中存储了密钥数据，请谨慎使用

3. **稳定性**
   - 由于微信Hook的特殊性，不保证在微信版本更新后仍然可用
   - 程序提供了异常处理和日志记录，请及时查看日志排查问题

## 常见问题

### Q: 运行时提示"检测到64位Python，但DLL是32位的"

A: 请安装32位Python并使用它运行程序。

### Q: 注入失败怎么办？

A: 请检查DLL文件是否存在，以及微信是否正在运行。

### Q: 如何修改监听的HTTP端口？

A: 可以通过设置环境变量`API_PORT`来修改端口。

## 扩展建议

1. 添加更多消息类型的处理
2. 实现更复杂的消息路由和过滤
3. 增加Web界面进行操作
4. 添加数据库支持持久化消息
5. 实现多机器人支持

## 许可证

该项目仅供学习和研究使用，请勿用于商业用途。使用该项目可能违反微信的用户协议，请谨慎使用。