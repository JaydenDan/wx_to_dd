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

   视频消息只使用抖音、快手、小红书和微博对应的专用解析器。专用解析器没有找到
   可下载视频时会直接跳过，不再进入 `WebMediaGrabber` 的通用浏览器兜底，因此不会
   因 CDN 403 重试刷屏或额外打开 Chromium 窗口。

抖音和小红书截图不再依赖固定等待时间：程序会等待活动播放器/笔记媒体真正出现在页面中后立即截图；
如果页面返回“作品/笔记不存在”、删除、下架、安全限制等状态，或超过 `MEDIA_READY_TIMEOUT` 仍没有媒体，
会记录“内容已丢失或不可访问”并跳过截图。

## 配置

请复制 `.env.example` 为 `.env` 并根据其中的注释配置您的环境。

需要查看 Playwright 浏览器窗口时，将 `.env` 中的 `PLAYWRIGHT_HEADLESS` 改为 `false`，然后重启服务。

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
