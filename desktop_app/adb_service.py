"""ADB 服务：发现私有 adb、接管/启动服务、设备列表解析、设备详情查询。

- adb 只从程序私有目录调用（platform-tools/adb.exe 或 COIN11_ADB_EXE 指向的绝对
  路径）；绝不依赖 PATH 里的系统 adb。
- 所有子进程调用带超时，输出结构化、可脱敏日志。
- 解析函数是纯函数，便于单元测试。
- 序列号/参数一律以列表形式传参并做字符白名单校验，杜绝路径/命令注入。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from . import constants
from .logutil import redact

logger = logging.getLogger("coin11.adb")

_SERIAL_RE = re.compile(r"^[A-Za-z0-9_\-\.\:]{1,64}$")


class AdbError(RuntimeError):
    """ADB 相关的可显示中文错误（带建议的下一步）。"""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


@dataclass
class DeviceInfo:
    serial: str
    state: str = "device"          # device / unauthorized / offline / recovery / ...
    product: Optional[str] = None
    model: Optional[str] = None
    device: Optional[str] = None
    usb: Optional[str] = None
    transport_id: Optional[str] = None

    @property
    def is_device(self) -> bool:
        return self.state == "device"

    @property
    def label(self) -> str:
        parts = [self.serial]
        if self.model:
            parts.append(self.model)
        return " ".join(p for p in parts if p)


def _tokenize_devices_line(line: str) -> Optional[dict]:
    """把一行 `adb devices -l` 输出解析为 dict；表头/空行返回 None。"""
    stripped = line.strip()
    if not stripped or stripped.startswith("List of devices"):
        return None
    cols = re.split(r"\s+", stripped)
    if len(cols) < 2:
        return None
    info = {"serial": cols[0], "state": cols[1]}
    for col in cols[2:]:
        if ":" in col:
            key, _, value = col.partition(":")
            info[key] = value
    return info


def parse_devices_l(output: str) -> List[DeviceInfo]:
    """解析 `adb devices -l` 的标准输出。纯函数，可单测。"""
    devices = []
    for line in output.splitlines():
        info = _tokenize_devices_line(line)
        if not info:
            continue
        devices.append(DeviceInfo(
            serial=info["serial"],
            state=info.get("state", "device"),
            product=info.get("product"),
            model=info.get("model"),
            device=info.get("device"),
            usb=info.get("usb"),
            transport_id=info.get("transport_id"),
        ))
    return devices


def validate_serial(serial: str) -> str:
    """校验并返回设备序列号；非法时抛 AdbError（防注入）。"""
    if not serial or not isinstance(serial, str) or not _SERIAL_RE.match(serial):
        raise AdbError("设备序列号格式非法，已拒绝执行。",
                       "请重新检测设备后重试。")
    return serial


class AdbService:
    """封装一次性的 adb 二进制定位与执行；短命令用超时，长任务由 TaskRunner 管理。

    并发控制：设备列表刷新按钮与任务运行期的后台看门狗共享同一 adb 服务，
    所有实际 adb 子进程由进程内锁串行化（排队而非并发），避免 UI 卡顿与
    无界 adb 调用堆叠。
    """

    def __init__(self, adb_exe: Optional[str] = None,
                 on_log: Optional[Callable[[str], None]] = None):
        self.adb_exe = self._locate(adb_exe)
        self.on_log = on_log or (lambda _text: None)
        self._lock = threading.Lock()
        self._log(f"使用 ADB: {self.adb_exe or '(未找到)'}")

    @staticmethod
    def _locate(adb_exe: Optional[str]) -> Optional[str]:
        if adb_exe:
            return os.path.abspath(adb_exe) if os.path.isfile(adb_exe) else None
        env = os.environ.get(constants.ENV_ADB_EXE)
        if env and os.path.isfile(env):
            return os.path.abspath(env)
        candidate = os.path.join(constants.app_base_dir(),
                                 constants.DIR_PLATFORM_TOOLS, "adb.exe")
        if os.path.isfile(candidate):
            return candidate
        return None

    def _log(self, text: str) -> None:
        safe = redact(text)
        logger.info(safe)
        self.on_log(safe)

    def available(self) -> bool:
        return bool(self.adb_exe)

    def version(self) -> str:
        return os.path.basename(self.adb_exe or "") or ""

    def run(self, args: List[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
        """运行 adb 子进程；失败抛 AdbError，输出脱敏并结构化记录。

        全部 adb 调用共享进程内锁串行执行：设备列表刷新与后台断线看门狗
        不会并发触发 adb，杜绝 UI 卡顿与无界 adb 进程堆积。
        """
        if not self.adb_exe:
            raise AdbError("未找到内置 ADB。",
                           "请确认发行目录包含 platform-tools/adb.exe，或重新安装本应用。")
        for a in args:
            if len(a) > 4096:
                raise AdbError("ADB 参数过长，已拒绝执行。", "")
        cmd = [self.adb_exe] + args
        with self._lock:
            self._log("$ adb " + " ".join(redact(x) for x in args))
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=timeout, creationflags=getattr(
                        subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired:
                raise AdbError("ADB 命令执行超时，已中止。",
                               "请稍后重试；若反复超时请检查 USB 连接。")
            except OSError as exc:
                raise AdbError(f"无法运行 ADB：{exc}", "请重新安装或修复本应用。")
        if proc.stdout:
            self._log(proc.stdout)
        if proc.stderr:
            self._log(proc.stderr)
        return proc

    # ---- 服务控制（启动页需向用户说明会中断共享 adb 服务的工具） ----

    def kill_server(self) -> None:
        self.run(["kill-server"], timeout=20.0)

    def start_server(self) -> None:
        self.run(["start-server"], timeout=30.0)

    def take_over(self) -> None:
        """重新接管 ADB：先 kill-server 再 start-server。执行前 GUI 必须提示。"""
        self.kill_server()
        self.start_server()

    def list_devices(self, timeout: float = 15.0) -> List[DeviceInfo]:
        proc = self.run(["devices", "-l"], timeout=timeout)
        return parse_devices_l(proc.stdout or "")

    def device_ready(self, serial: str, timeout: float = 6.0) -> bool:
        """检查指定 serial 是否仍处于可用（device）状态。看门狗专用。

        单次检查带超时；ADB 服务瞬时错误 / 命令超时按“不可用”返回 False，
        由调用方按连续失败次数判定，避免单次抖动误停。
        非法 serial（注入风险）直接抛 AdbError，不做静默降级。
        """
        if not serial:
            return False
        validate_serial(serial)
        if not self.available():
            return False
        try:
            devices = self.list_devices(timeout=timeout)
        except (AdbError, OSError):
            return False
        for dev in devices:
            if dev.serial == serial:
                return dev.is_device
        return False

    # ---- 设备详情 ----

    def _getprop(self, serial: str, key: str) -> str:
        validate_serial(serial)
        proc = self.run(["-s", serial, "shell", "getprop", key], timeout=15.0)
        return (proc.stdout or "").strip()

    def device_properties(self, serial: str) -> dict:
        """查询型号 / 品牌 / Android 版本。失败返回空值，不抛中断。"""
        out = {}
        for key, name in (
                ("ro.product.brand", "brand"),
                ("ro.product.model", "model"),
                ("ro.build.version.release", "android_version"),
                ("ro.product.manufacturer", "manufacturer")):
            try:
                out[name] = self._getprop(serial, key) or ""
            except AdbError as exc:
                logger.warning("getprop %s 失败: %s", key, exc.message)
        return out

    def connection_state(self) -> dict:
        """汇总当前连接状态：adbc 是否存在、设备列表、状态映射与下一步提示。"""
        if not self.available():
            return {"adbc": False, "state": "no_adb", "devices": [],
                    "message": "未找到内置 ADB 程序。",
                    "hint": "请重新安装本应用，或把 platform-tools 放到程序目录后重启。"}
        try:
            devices = self.list_devices()
        except AdbError as exc:
            return {"adbc": True, "state": "error", "devices": [],
                    "message": exc.message, "hint": exc.hint}
        if not devices:
            return {"adbc": True, "state": "no_device", "devices": [],
                    "message": "未检测到任何 USB 调试设备。",
                    "hint": "请用 USB 数据线连接手机，开启“开发者选项”里的“USB 调试”，"
                           "然后点击“重新检测”。"}
        ready = [d for d in devices if d.is_device]
        unauthorized = [d for d in devices if d.state == "unauthorized"]
        offline = [d for d in devices if d.state == "offline"]
        if not ready and unauthorized:
            return {"adbc": True, "state": "unauthorized", "devices": devices,
                    "message": "检测到设备但未获授权（unauthorized）。",
                    "hint": "请在手机上解锁屏幕，找到 USB 调试授权弹窗：勾选"
                           "“始终允许来自此计算机的调试”后点“允许”，再重新检测。"}
        if not ready and offline:
            return {"adbc": True, "state": "offline", "devices": devices,
                    "message": "检测到设备但状态为离线（offline）。",
                    "hint": "请重新插拔 USB 线，并确认没有其他程序正在独占 ADB。"}
        if not ready:
            return {"adbc": True, "state": "no_device", "devices": devices,
                    "message": "未检测到可用的 USB 调试设备。",
                    "hint": "请检查驱动与 USB 调试开关后重新检测。"}
        return {"adbc": True, "state": "device", "devices": devices,
                "message": f"已连接 {len(ready)} 台设备。",
                "hint": ""}
