# 微信消息转发钉钉 & 视频自动下载发送

本项目用于监听微信消息，自动解析其中的视频链接（支持抖音、快手、小红书、微博等），下载视频并转发到钉钉。

## 安装说明

在开始使用之前，请确保已安装 Python 32位环境, 因为 wxhelper dll 只支持 32位, 但是某些组件没有 32位版本, 我们特别挑选性安装, 先安把有 32 位版本的组件都安装好, 然后再无依赖安装其他组件(vediofatch)

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

## 运行

```bash
python main.py
```
