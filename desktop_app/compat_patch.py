"""受控兼容补丁（只作用于发行副本/同步副本，不修改仓库里的上游业务逻辑）。

1. utils.select_device()：支持 COIN11_DEVICE_SERIAL —— 存在且处于 device 状态时
   直接返回该序列号；否则完全保持上游命令行行为（多设备交互选择）。幂等。
2. 不使用 select_device 的上游脚本（如 `d = u2.connect()`）补丁：当环境中存在
   COIN11_DEVICE_SERIAL 时按该序列号定向连接；无该变量时行为与上游一致。
"""
from __future__ import annotations

import os
import re
from typing import Optional

SELECT_DEVICE_MARKER = "COIN11_DEVICE_SERIAL"

# 插入到 utils.select_device() 函数体最前的受控补丁
_UTILS_PATCH = '''
    # === Coin11 桌面版受控兼容补丁（BEGIN）===
    # 桌面应用通过环境变量注入明确设备：存在且处于 device 状态时直接返回，
    # 不改动命令行/交互式用户的原有行为。
    import os as _coin11_os
    _coin11_serial = _coin11_os.environ.get("COIN11_DEVICE_SERIAL")
    if _coin11_serial:
        if _coin11_serial in get_connected_devices():
            set_terminal_title(_coin11_serial)
            return _coin11_serial
    # === Coin11 桌面版受控兼容补丁（END）===
'''

_UTILS_ANCHOR = "def select_device():\n"
_UTILS_NEXT = "    # 获取所有连接的设备\n"

# u2.connect() 无设备定向脚本的补丁
_U2_IMPORT_MARKER = "import os as _coin11_os"
_U2_OLD = "d = u2.connect()"
_U2_NEW = (
    "import os as _coin11_os\n"
    "if _coin11_os.environ.get(\"COIN11_DEVICE_SERIAL\"):\n"
    "    d = u2.connect(_coin11_os.environ[\"COIN11_DEVICE_SERIAL\"])\n"
    "else:\n"
    "    d = u2.connect()\n"
)

# utils.py EasyOCR 桌面兼容补丁：上游默认 gpu=True 且联网下载模型；
# 完整运行时发行版内置 CPU torch + 离线模型目录，补丁后默认 gpu=False，
# 并在 COIN11_EASYOCR_MODEL_DIR 存在时用离线模型（禁联网）。幂等。
_EASYOCR_MARKER = "COIN11_EASYOCR_MODEL_DIR"
_EASYOCR_OLD = "easyocr_reader = easyocr.Reader(['ch_sim', 'en'], gpu=True)"
_EASYOCR_NEW = (
    "import os as _coin11_runtime_os\n"
    "COIN11_EASYOCR_MODEL_DIR = _coin11_runtime_os.environ.get(\n"
    "    \"COIN11_EASYOCR_MODEL_DIR\", \"\")\n"
    "COIN11_EASYOCR_KWARGS = {'gpu': False}\n"
    "if COIN11_EASYOCR_MODEL_DIR:\n"
    "    COIN11_EASYOCR_KWARGS['model_storage_directory'] = COIN11_EASYOCR_MODEL_DIR\n"
    "    COIN11_EASYOCR_KWARGS['download_enabled'] = False\n"
    "easyocr_reader = easyocr.Reader(['ch_sim', 'en'], **COIN11_EASYOCR_KWARGS)"
)


def patch_utils_select_device(source: str) -> str:
    """对 utils.py 文本应用 select_device 兼容补丁（幂等）。"""
    if SELECT_DEVICE_MARKER in source:
        return source  # 已包含补丁
    idx = source.find(_UTILS_ANCHOR)
    if idx < 0:
        raise ValueError("utils.py 中未找到 select_device 定义，无法打补丁")
    anchor_end = idx + len(_UTILS_ANCHOR)
    body = source[anchor_end:]
    # 保留 def 后到首个代码语句之间的注释，把补丁插在首个代码语句之前
    code_offset = 0
    for line in body.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            code_offset += len(line)
            continue
        break
    return source[:anchor_end] + body[:code_offset] + _UTILS_PATCH + body[code_offset:]


# 使用裸 u2.connect()（无设备定向）的受支持脚本：0.3.0 起桌面按文件名白名单
# 打补丁（含限时活动脚本，页面改版只影响任务是否成功，不影响设备定向）。
_U2_CONNECT_SCRIPTS = frozenset({
    "闲鱼扔骰子.py",
    "支付宝农场.py",
    "闲鱼现金签到.py",
    "支付宝打卡.py",
    "2024淘宝双11.py",
    "2025淘宝618活动.py",
})


def patch_u2_connect(source: str, filename: str = "") -> str:
    """对脚本副本应用 u2.connect() 设备定向补丁（幂等）。返回补丁后的文本。

    filename 仅用于报错信息。
    """
    if _U2_IMPORT_MARKER in source:
        return source
    if _U2_OLD not in source:
        # 无 `d = u2.connect()` 的脚本无需该补丁（可能已用 select_device）
        return source
    return source.replace(_U2_OLD, _U2_NEW, 1)


def patch_utils_easyocr(source: str) -> str:
    """对 utils.py 文本应用 EasyOCR 桌面兼容补丁（gpu=False + 离线模型目录）。

    幂等：已含 COIN11_EASYOCR_MODEL_DIR 标记时原样返回。
    兼容两种书写形态：
      a) easyocr_reader = easyocr.Reader(['ch_sim', 'en'], gpu=True)
      b) easyocr_reader = easyocr.Reader(['ch_sim', 'en'], **kwargs)（含我们补丁后的形态）
    找不到该行时静默返回原文（不因上游改版误伤同步）。
    """
    if _EASYOCR_MARKER in source:
        return source
    if _EASYOCR_OLD not in source:
        return source
    return source.replace(_EASYOCR_OLD, _EASYOCR_NEW, 1)


def needs_u2_patch(script_text: str) -> bool:
    """判断副本是否还需要设备定向补丁（不含 select_device 且含裸 u2.connect()）。"""
    return ("select_device" not in script_text) and (_U2_OLD in script_text)


def apply_known_patches(filename: str, text: str) -> str:
    """按文件名应用已知受控补丁（幂等、白名单式）。

    仅处理 desktop_app 自身认可的受支持脚本/模块；其它文件原样返回。
    受支持但使用 select_device()/无裸 connect 的脚本无需补丁。
    """
    base = os.path.basename(filename)
    if base == "utils.py":
        text = patch_utils_select_device(text)
        return patch_utils_easyocr(text)
    if base in _U2_CONNECT_SCRIPTS:
        return patch_u2_connect(text, filename)
    return text
