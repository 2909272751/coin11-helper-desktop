# Coin11 助手桌面版 —— 受控兼容补丁说明

本发行目录中的 `utils.py` 及个别任务脚本相对上游
<https://github.com/czl0325/coin11-tb>（Apache-2.0）仅做了以下**受控兼容补丁**，
不改动任何任务脚本的业务逻辑：

## 1. `utils.select_device()`：支持桌面环境变量（`utils.py`）

函数体开头插入一段幂等补丁（标记 `Coin11 桌面版受控兼容补丁`）：

- 读取环境变量 `COIN11_DEVICE_SERIAL`；
- 若该变量存在，且对应序列号出现在 `adb devices` 的 `device` 状态列表中，
  直接返回该序列号（桌面应用把用户选择的设备注入此变量）；
- 否则完全走上游原有逻辑（命令行/交互式用户行为不变）。

## 2. 少数不使用 `select_device` 的上游脚本：定向连接

`闲鱼扔骰子.py`、`支付宝农场.py` 原本直接 `d = u2.connect()`，桌面运行时
uiautomator2 无法知道要连哪台设备。补丁改为：

- 环境存在 `COIN11_DEVICE_SERIAL` 时 `u2.connect(<该序列号>)`；
- 无该变量时保持上游默认行为不变。

## 3. `utils.py` EasyOCR：CPU + 离线模型目录（0.2.0）

上游默认 `easyocr.Reader(['ch_sim','en'], gpu=True)` 且联网下载模型；桌面发行版
内置 CPU 版 PyTorch 与离线模型目录 `runtime\easyocr-models`。补丁改为：

- EasyOCR 默认 `gpu=False`（CPU 推理）；
- 环境变量 `COIN11_EASYOCR_MODEL_DIR` 存在时，用其作为 `model_storage_directory`
  并关闭 `download_enabled`（离线模型，不联网）；
- 未设置该变量时保持上游可运行默认（联网自动下载）—— 不影响命令行/交互式使用。

> 仓库根 `utils.py` 已带此兼容入口（同 `select_device` 补丁），同步到本机的
> 上游副本在构建/同步时也会自动应用同一补丁。

## 应用时机

补丁只在**构建出厂种子**与**同步下载校验后**应用到副本上，仓库源文件
（除 `utils.py` 一处兼容入口外）不被改写；补丁幂等，重复应用无副作用。
