# 微信消息转发钉钉 & 视频自动下载发送

本项目用于监听微信消息，自动解析其中的视频链接（支持抖音、快手、小红书、微博等），下载视频并转发到钉钉。

当前版本已切换为 `vx_hook_4.2.8.28` 的 `HTTP` 回调模式，不再使用 `4.1.2.17` 的 `Loader/Helper DLL` 直连方案。
当前接收端已改为 `FastAPI + uvicorn` 架构，本程序仅负责启动本地回调服务，等待外部已运行的 vxhook 把消息推送过来，不负责注入微信或拉起 hook。

## 安装说明

1. 安装项目依赖：

   ```bash
   pip install -r requirements.txt
   ```

2. **特别注意**：需要额外安装 `videofetch` 且不安装其依赖（为了避免冲突）：

   ```bash
   pip install videofetch --no-deps
   ```

## 配置

请复制 `.env.example` 为 `.env` 并根据其中的注释配置您的环境。

当前只需要配置本地回调服务地址：

- `VXHOOK_CALLBACK_HOST`
- `VXHOOK_CALLBACK_PORT`
- `VXHOOK_CALLBACK_PATH`

外部 vxhook 需要把 HTTP 回调地址指向：

- `http://127.0.0.1:5000/api/recvMsg`

如果你修改了监听地址或路径，请同步修改 vxhook 侧的回调配置。

## 运行

```bash
python main.py
```

启动后会监听以下回调：

- `POST /api/recvMsg`
- `PUT /api/recvMsg`
- `GET /healthz`
