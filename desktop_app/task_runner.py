"""任务运行器：用独立子进程跑选中的上游脚本。

- 每次运行注入 COIN11_DEVICE_SERIAL（utils.select_device 的兼容入口）。
- 实时把子进程 stdout/stderr 逐行、低延迟转发给 GUI 日志（每行立即回调，
  不等子进程退出）；同时保留最近输出的环形缓冲用于诊断。
- 支持安全停止进程树（Windows taskkill /T /F，绝不按模糊进程名批量杀）。
- 断线保护：运行期间周期检查所选设备（约 2 秒，单次检查带超时），设备连续
  两次缺失 / offline / unauthorized 时写明确提示并安全停止整个任务进程树，
  结果标记为 cancelled（设备断开），且不继续队列中的后续任务。
- 状态机：idle -> running -> success/failed/cancelled/skipped/completed；
  禁止把“退出码 0 + 脚本内 while 无限循环被我们自己中断”伪装成真实手机任务成功：
  桌面层只报告『脚本进程正常退出』，任务是否真的在手机上完成需要用户人工确认。
- 运行前校验：仅能运行 TaskCatalog.resolve 返回的受控绝对路径。
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional

from . import constants
from .logutil import redact
from .runtime import easyocr_model_dir, runtime_python_exe

logger = logging.getLogger("coin11.runner")

# 看门狗默认：约 2 秒检查一次；连续 watch_miss_limit 次失败才判定断开。
WATCH_INTERVAL_DEFAULT = 2.0
WATCH_MISS_LIMIT_DEFAULT = 2


def resolve_task_python(python_exe: Optional[str] = None) -> str:
    """决定用于运行上游任务脚本的解释器路径；不可用返回 ""（由调用方报错）。

    - 显式传入（测试/调用方覆盖）优先；
    - 否则用 runtime_python_exe()：冻结发行选择发行内置解释器
      （runtime\\python\\python.exe 或等效已验证解释器）；源码模式用当前解释器。
    绝不返回 EXE 自身（sys.executable 冻结后是 Coin11助手.exe，递归启动无意义）。
    """
    if python_exe:
        return python_exe
    return runtime_python_exe()


def build_task_env(device_serial: str = "",
                   python_exe: str = "",
                   script_dir: str = "") -> dict:
    """构造任务脚本的子进程环境。

    - 注入 COIN11_DEVICE_SERIAL（设备定向，utils.select_device 兼容入口）；
    - 注入 COIN11_EASYOCR_MODEL_DIR（离线模型目录存在时）—— EasyOCR 离线运行；
    - 把内置解释器/私有 adb 所在目录放到 PATH 开头（随附可执行文件可用，
      不依赖用户 PATH）；不污染全局环境。
    """
    env = dict(os.environ)
    if device_serial:
        env[constants.ENV_DEVICE_SERIAL] = device_serial
    env["PYTHONIOENCODING"] = "utf-8"
    model_dir = easyocr_model_dir()
    if model_dir:
        env[constants.ENV_EASYOCR_MODEL_DIR] = model_dir
    path_prepend = []
    if python_exe:
        interp_dir = os.path.dirname(os.path.abspath(python_exe))
        if interp_dir and interp_dir not in path_prepend:
            path_prepend.append(interp_dir)
    adb = os.path.join(constants.app_base_dir(), constants.DIR_PLATFORM_TOOLS)
    if os.path.isdir(adb) and adb not in path_prepend:
        path_prepend.append(adb)
    if path_prepend:
        old = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(path_prepend + ([old] if old else []))
    return env


class RunState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCESS = "success"        # 脚本进程退出且退出码为 0
    FAILED = "failed"          # 脚本进程异常/退出码非 0
    CANCELLED = "cancelled"    # 用户停止 / 设备断开自动停止
    SKIPPED = "skipped"        # 任务不可用（脚本缺失等）或队列停止后未运行
    COMPLETED = "completed"    # 一个任务列表整体跑完（≠ 手机任务全成功）


@dataclass
class TaskOutcome:
    task_id: str
    title: str
    state: str = RunState.IDLE.value
    exit_code: Optional[int] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    note: str = ""

    @property
    def duration(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)


class _RingBuffer:
    """有界最近输出缓冲（线程安全）：诊断用，保留最近 N 行。"""

    def __init__(self, max_lines: int = 1000, max_bytes: int = 262144):
        self.max_lines = max_lines
        self.max_bytes = max_bytes
        self._lines: List[str] = []
        self._bytes = 0
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)
            self._bytes += len(line)
            while (len(self._lines) > self.max_lines
                   or self._bytes > self.max_bytes) and self._lines:
                dropped = self._lines.pop(0)
                self._bytes -= len(dropped)

    def snapshot(self) -> List[str]:
        with self._lock:
            return list(self._lines)

    def drain(self) -> List[str]:
        with self._lock:
            lines = self._lines
            self._lines = []
            self._bytes = 0
            return lines

    def clear(self) -> None:
        with self._lock:
            self._lines = []
            self._bytes = 0


class SafeSubprocess:
    """可取消、可超时、实时逐行转发输出的子进程封装。

    读取线程对 stdout（stderr 合并到 stdout 保持顺序）逐行读取：每行立即回调
    on_line，同时写入最近输出缓冲（诊断用），绝不等到子进程退出才回调。
    UTF-8 解码 + errors=replace；上游脚本以 `-u` 无缓冲运行，读端逐行即时。
    """

    def __init__(self, cmd: List[str], cwd: str, env: dict, timeout: float = 0.0):
        self.cmd = cmd
        self.cwd = cwd
        self.env = env
        self.timeout = timeout
        self.proc: Optional[subprocess.Popen] = None
        self._kill_evt = threading.Event()
        self._buf = _RingBuffer()

    def start(self) -> None:
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x08000000
        self.proc = subprocess.Popen(
            self.cmd, cwd=self.cwd, env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1,  # 行缓冲，避免读端攒批
            creationflags=creationflags)

    def _read_loop(self, on_line: Callable[[str], None]) -> None:
        """逐行读取并即时回调（reader 线程内回调；调用方需保证线程安全）。"""
        assert self.proc is not None
        try:
            while True:
                line = self.proc.stdout.readline()  # type: ignore[attr-defined]
                if line == "":
                    break
                self._buf.append(line)
                if on_line is not None:
                    try:
                        on_line(line)
                    except Exception:  # noqa: BLE001
                        logger.exception("on_line 回调异常")
        except (ValueError, OSError):
            pass
        finally:
            try:
                if self.proc.stdout:
                    self.proc.stdout.close()  # type: ignore[attr-defined]
            except (ValueError, OSError):
                pass

    def stream(self, on_line: Callable[[str], None]) -> int:
        """读取输出并逐行实时回调，直到退出、stop() 或超时。返回退出码。

        退出后还会把读取线程尚未回吐的剩余缓冲行补发（尽力而为）。
        """
        reader = threading.Thread(target=self._read_loop, args=(on_line,),
                                  daemon=True)
        reader.start()
        assert self.proc is not None
        deadline = time.time() + self.timeout if self.timeout > 0 else None
        while True:
            if self.proc.poll() is not None:
                break
            if self._kill_evt.is_set():
                self._terminate_tree()
                break
            if deadline and time.time() > deadline:
                self._terminate_tree()
                break
            time.sleep(0.05)
        # 尽量把读取线程未消费完的行吐完（reader 已即时回调，此处为收尾兜底）
        try:
            reader.join(timeout=5)
        except RuntimeError:
            pass
        for line in self._buf.drain():
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:  # noqa: BLE001
                    logger.exception("on_line 回调异常")
        return int(self.proc.wait(timeout=5) if self.proc.poll() is None
                   else self.proc.returncode or 0)

    def recent_output(self) -> str:
        """最近输出缓冲（诊断用，线程安全）。"""
        return "".join(self._buf.snapshot())

    def _terminate_tree(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                    capture_output=True, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.proc.kill()
                except OSError:
                    pass
        else:
            try:
                os.killpg(os.getpgid(self.proc.pid), 9)  # type: ignore[attr-defined]
            except (OSError, AttributeError):
                try:
                    self.proc.kill()
                except OSError:
                    pass

    def stop(self) -> None:
        self._kill_evt.set()


class TaskRunner:
    """串行执行一个任务列表，每次一个设备（单实例一次只跑一个任务）。

    可注入 device_watch(serial) -> bool：运行期间由看门狗线程周期性调用，
    返回 False 表示设备当前不可用；连续 watch_miss_limit 次不可用即判定断开。
    """

    def __init__(self, task_catalog, device_serial: str = "",
                 on_log: Optional[Callable[[str], None]] = None,
                 on_state: Optional[Callable[[dict], None]] = None,
                 on_progress: Optional[Callable[[dict], None]] = None,
                 device_watch: Optional[Callable[[str], bool]] = None,
                 watch_interval: float = WATCH_INTERVAL_DEFAULT,
                 watch_miss_limit: int = WATCH_MISS_LIMIT_DEFAULT):
        self.catalog = task_catalog
        self.device_serial = device_serial
        self.on_log = on_log or (lambda _text: None)
        self.on_state = on_state or (lambda _s: None)
        self.on_progress = on_progress or (lambda _p: None)
        self.device_watch = device_watch
        self.watch_interval = watch_interval
        self.watch_miss_limit = max(1, int(watch_miss_limit))
        self._current: Optional[SafeSubprocess] = None
        self._state = RunState.IDLE.value
        self._stop_requested = False      # 用户手动停止
        self._device_lost = False         # 设备断开自动停止
        self.results: List[TaskOutcome] = []
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        return self._state

    def _set_state(self, state: str) -> None:
        with self._lock:
            self._state = state
        self.on_state({"state": state})

    def _emit(self, text: str) -> None:
        safe = redact(text.rstrip("\n"))
        if safe:
            self.on_log(safe)

    def stop(self) -> None:
        """请求停止当前任务与队列（安全停止进程树，且不继续后续任务）。"""
        self._stop_requested = True
        cur = self._current
        if cur:
            cur.stop()

    def running(self) -> bool:
        return self._state == RunState.RUNNING.value

    def run_tasks(self, task_ids: List[str], python_exe: str = "",
                  timeout_per_task: float = 0.0) -> List[TaskOutcome]:
        """按给定顺序运行任务（受控校验），返回每个任务的结果。阻塞调用。

        python_exe 为空时使用 runtime_python_exe() 选择发行内置解释器；
        找不到解释器时在启动前给出明确失败，绝不递归启动桌面 EXE。
        用户停止或设备断开后不再启动后续任务；未运行的任务记为 skipped。
        """
        if self._state == RunState.RUNNING.value:
            raise RuntimeError("已有任务正在运行")
        ids = self.catalog.validate_ids(list(task_ids))
        py = resolve_task_python(python_exe or None)
        total = len(ids)
        self.results = []
        self._stop_requested = False
        self._device_lost = False
        if not py:
            self._set_state(RunState.RUNNING.value)
            self._emit("[错误] 未找到内置 Python 运行时（runtime\\python\\python.exe）。"
                       "请重新安装完整发行版，或确认发行目录完整。")
            for task_id in ids:
                outcome = TaskOutcome(task_id, task_id, state=RunState.FAILED.value)
                outcome.note = "缺少内置 Python 运行时"
                outcome.exit_code = -1
                self.results.append(outcome)
            self._set_state(RunState.COMPLETED.value)
            return list(self.results)
        self._set_state(RunState.RUNNING.value)
        try:
            for index, task_id in enumerate(ids, start=1):
                if (self._stop_requested or self._device_lost
                        or self._state == RunState.CANCELLED.value):
                    break
                self.on_progress({"state": "task_start", "task": task_id,
                                  "index": index, "total": total})
                outcome = self._run_one(task_id, py, timeout_per_task, index,
                                        total)
                self.results.append(outcome)
                if outcome.state in (RunState.CANCELLED.value,
                                     RunState.SKIPPED.value) and (
                        self._stop_requested or self._device_lost):
                    break
                self.on_progress({"state": "task_end", "task": task_id,
                                  "index": index, "total": total,
                                  "result": outcome})
            if self._stop_requested or self._device_lost:
                # 队列已停止：剩余未运行任务标 skipped（注明未运行），不启动
                self._emit("[提示] 队列已停止，不再运行后续任务。")
                run_ids = {o.task_id for o in self.results}
                for task_id in ids:
                    if task_id in run_ids:
                        continue
                    entry = self.catalog.get(task_id) or {"title": task_id}
                    o = TaskOutcome(task_id, entry.get("title", task_id),
                                    state=RunState.SKIPPED.value)
                    o.note = "未运行（队列已停止）"
                    self.results.append(o)
            cancelled = (self._stop_requested or self._device_lost
                         or self._state == RunState.CANCELLED.value)
            self._set_state(RunState.CANCELLED.value if cancelled
                            else RunState.COMPLETED.value)
        finally:
            if self._state == RunState.RUNNING.value:
                self._set_state(RunState.COMPLETED.value)
        return list(self.results)

    def _watch_loop(self, sub: SafeSubprocess,
                    stop_evt: threading.Event) -> None:
        """看门狗：周期检查设备；连续 watch_miss_limit 次不可用则停止任务。"""
        misses = 0
        while not stop_evt.is_set():
            if sub._kill_evt.is_set():
                return  # 已停止（用户手动），不误报设备断开
            try:
                ok = bool(self.device_watch(self.device_serial))
            except Exception:  # noqa: BLE001
                logger.exception("设备检查异常，按不可用处理")
                ok = False
            if not ok:
                misses += 1
                if misses >= self.watch_miss_limit:
                    self._device_lost = True
                    self._emit("[设备] 设备已断开（连续未检测到可用状态），"
                               "已自动停止当前任务并停止整个队列。")
                    self._emit("[操作] 请检查 USB 连接与授权后，重新开始运行。")
                    sub.stop()
                    return
                # 缩短等待，尽快完成“连续两次”确认，避免误报拖延
                if stop_evt.wait(0.5):
                    return
                continue
            misses = 0
            stop_evt.wait(self.watch_interval)

    def _run_one(self, task_id: str, python_exe: str,
                 timeout_per_task: float, index: int = 1,
                 total: int = 1) -> TaskOutcome:
        try:
            script = self.catalog.resolve(task_id)
        except ValueError as exc:
            logger.warning("跳过任务 %s: %s", task_id, exc)
            self._emit(f"[跳过] {exc}")
            entry = self.catalog.get(task_id) or {"title": task_id}
            outcome = TaskOutcome(task_id, entry.get("title", task_id),
                                  state=RunState.FAILED.value)
            outcome.note = str(exc)
            return outcome
        entry = self.catalog.get(task_id) or {"title": task_id}
        title = entry.get("title", task_id)
        outcome = TaskOutcome(task_id, title)
        outcome.started_at = time.time()
        self._emit(f"[开始] {title}")
        if not python_exe:
            outcome.state = RunState.FAILED.value
            outcome.note = "缺少内置 Python 运行时"
            self._emit("[错误] 未找到内置 Python 运行时，任务未启动。")
            outcome.finished_at = time.time()
            return outcome
        # 工作目录 = 脚本所在目录（脚本内的 ./img 相对路径依赖它）
        cwd = os.path.dirname(script)
        env = build_task_env(self.device_serial, python_exe, cwd)
        sub = SafeSubprocess([python_exe, "-u", script], cwd=cwd, env=env,
                             timeout=timeout_per_task)
        self._current = sub
        sub.start()
        self.on_state({"state": RunState.RUNNING.value, "task": title,
                       "task_id": task_id, "index": index, "total": total})
        watch_stop = threading.Event()
        watch_thread: Optional[threading.Thread] = None
        if self.device_serial and self.device_watch:
            watch_thread = threading.Thread(
                target=self._watch_loop, args=(sub, watch_stop), daemon=True)
            watch_thread.start()
        try:
            exit_code = sub.stream(self._emit)
        finally:
            watch_stop.set()
            if watch_thread is not None:
                watch_thread.join(timeout=3)
            self._current = None
        if self._device_lost:
            outcome.state = RunState.CANCELLED.value
            outcome.note = "设备已断开，已自动停止任务"
            self._emit(f"[已停止] {title}：设备已断开，已自动停止任务。")
        elif self._stop_requested or sub._kill_evt.is_set():
            outcome.state = RunState.CANCELLED.value
            outcome.note = "用户停止"
            self._emit(f"[已停止] {title}")
        elif exit_code == 0:
            outcome.state = RunState.SUCCESS.value
            self._emit(f"[完成] {title}：脚本进程正常结束。"
                       f"（是否真正在手机上完成任务，请以手机界面为准）")
        else:
            outcome.state = RunState.FAILED.value
            self._emit(f"[失败] {title}：脚本进程退出码 {exit_code}。")
        outcome.exit_code = exit_code
        outcome.finished_at = time.time()
        return outcome
