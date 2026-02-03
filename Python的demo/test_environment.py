# -*- coding: utf8 -*-

import sys
import os
import ctypes
from ctypes import WinDLL

# 打印基本信息
print(f"Python版本: {sys.version}")
print(f"是否64位Python: {sys.maxsize > 2**32}")
print(f"当前目录: {os.getcwd()}")

# 检查文件是否存在
loader_path = "./Loader_4.1.2.17.dll"
dll_path = "./Helper_4.1.2.17.dll"
print(f"Loader.dll存在: {os.path.exists(loader_path)}")
print(f"Helper.dll存在: {os.path.exists(dll_path)}")

# 尝试加载DLL
try:
    print("尝试加载Loader.dll...")
    loader = WinDLL(loader_path)
    print("Loader.dll加载成功!")
except Exception as e:
    print(f"加载Loader.dll失败: {e}")
    print(f"错误类型: {type(e).__name__}")