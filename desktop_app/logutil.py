"""本地日志与脱敏工具。

日志只写入用户本地目录，绝不外发。GUI 显示的诊断文本与落盘日志都经过脱敏。
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import re

# 手机号：+86 / 86 / 1[3-9]xxxxxxxxx 形态；统一替换为 <手机号>
_RE_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
# 常见 token / 授权串形态（十六进制或 base64 风格长串）
_RE_TOKEN = re.compile(r"(?i)(token|secret|authorization|auth|sign|deviceid|serial)(['\"\s:=]+)([A-Za-z0-9_\-\.]{16,})")
_RE_LONG = re.compile(r"[A-Za-z0-9_\-\.]{24,}")
_RE_SERIAL = re.compile(r"(?i)(serial|device)(['\"\s:=]+)([A-Za-z0-9_\-]{6,})")

_SENSITIVE_HINT = re.compile(
    r"(?i)(手机号|验证码|登录|支付|付款|token|密钥|授权|密码|serial|authorization|auth|secret)"
)


def redact(text: str) -> str:
    """把日志/输出中的常见敏感信息替换为占位符。纯函数，可单测。"""
    if not text:
        return text
    out = _RE_PHONE.sub("<手机号>", text)
    out = _RE_TOKEN.sub(lambda m: m.group(1) + m.group(2) + "<已脱敏>", out)
    out = _RE_SERIAL.sub(lambda m: m.group(1) + m.group(2) + "<已脱敏>", out)
    # 剩余的长串（可能为登录态/设备授权串）也脱敏，但保留明显无害的 commit/版本号形态
    out = _RE_LONG.sub(_long_keep, out)
    return out


def _long_keep(m: re.Match) -> str:
    token = m.group(0)
    # 40 位十六进制视为 commit sha / 64 位以上视为凭据；普通版本号/短标识保留
    if re.fullmatch(r"[0-9a-f]{40}", token):
        return token
    return "<长串已脱敏>"


def should_hold_back(text: str) -> bool:
    """判断日志行是否包含提示“需人工处理”的敏感词（登录/验证码/支付/授权）。"""
    return bool(text) and bool(_SENSITIVE_HINT.search(text))


# “仅看错误”过滤：行内包含以下标记之一视为错误/异常行。
_ERR_MARKERS = (
    "[错误]", "error", "Error", "ERROR", "Traceback", "traceback",
    "Exception", "exception", "  File \"", "退出码",
)


def is_error_line(text: str) -> bool:
    """判断日志行是否需要显示在“仅看错误”过滤视图下。纯函数，可单测。"""
    if not text:
        return False
    return any(marker in text for marker in _ERR_MARKERS)


def setup_logging(log_dir: str) -> str:
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "app.log")
    logger = logging.getLogger("coin11")
    if not logger.handlers:
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.INFO)
    return path


def log_path(log_dir: str) -> str:
    return os.path.join(log_dir, "app.log")
