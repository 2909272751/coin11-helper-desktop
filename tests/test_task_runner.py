# -*- coding: utf-8 -*-
"""任务命令构造 / 路径白名单 / 运行状态机 / 实时日志与断线看门狗单元测试。"""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # helpers

from desktop_app import constants  # noqa: E402
from desktop_app.task_catalog import TaskCatalog, build_task_index  # noqa: E402
from desktop_app.task_runner import (  # noqa: E402
    RunState,
    SafeSubprocess,
    TaskOutcome,
    TaskRunner,
)

# 工具/测试/旧命令行批处理器：绝不进入任务清单
_FORBIDDEN_SCRIPTS = {"chromedriver.py", "识别图片测试.py", "淘宝多任务执行.py"}


class TaskCatalogTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, content="# dummy\n"):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as fh:
            fh.write(content)

    def test_default_tasks_known_and_safe_names(self):
        index = build_task_index(self.root)
        expected_ids = {s["id"] for s in constants.ALL_TASKS}
        self.assertEqual(set(index), expected_ids)
        # 原有 6 个日常任务仍在表内
        old = {"taobao_achievement", "xianyu_dice", "taobao_baba_farm",
               "taobao_coin", "tmall_tree", "alipay_farm"}
        self.assertTrue(old.issubset(set(index)))
        # 0.3.0 新增的日常任务 ID
        added = {"taobao_cash_checkin", "xianyu_cash_checkin",
                 "alipay_checkin", "wanzhuan_alipay", "boxian_checkin"}
        self.assertTrue(added.issubset(set(index)))
        # 限时活动纳入
        limited_ids = {s["id"] for s in constants.LIMITED_TASKS}
        self.assertTrue(limited_ids.issubset(set(index)))
        self.assertEqual(len(constants.LIMITED_TASKS), 4)
        for entry in index.values():
            # 文件名必须是白名单安全形态
            self.assertNotIn("..", entry["script"])
            self.assertNotIn("/", entry["script"])
            self.assertNotIn("\\", entry["script"])
        # 任务表不得包含工具/测试/旧命令行批处理器脚本
        scripts = {e["script"] for e in index.values()}
        self.assertFalse(scripts & _FORBIDDEN_SCRIPTS)

    def test_new_daily_and_limited_entries_resolvable(self):
        # 写入全部受支持脚本后，新 ID 应可运行（resolve 通过、exists=True）
        for spec in constants.ALL_TASKS:
            self._write(spec["script"])
        cat = TaskCatalog(self.root)
        for spec in constants.ALL_TASKS:
            self.assertTrue(cat.get(spec["id"])["exists"], spec["id"])
            path = cat.resolve(spec["id"])
            self.assertTrue(os.path.isfile(path))
        self.assertTrue(cat.is_limited("limited_2024_double11"))
        self.assertFalse(cat.is_limited("taobao_achievement"))
        # 未纳入的任务 ID 一律拒绝
        with self.assertRaises(ValueError):
            cat.resolve("chromedriver")

    def test_missing_script_not_runnable(self):
        self._write("淘宝成就中心签到.py")
        index = build_task_index(self.root)
        self.assertTrue(index["taobao_achievement"]["exists"])
        self.assertFalse(index["xianyu_dice"]["exists"])
        with self.assertRaises(ValueError):
            TaskCatalog(self.root).resolve("xianyu_dice")

    def test_resolve_rejects_unknown_or_injection_id(self):
        cat = TaskCatalog(self.root)
        for bad in ("../outside", "os", "&calc", "淘宝成就中心签到"):
            with self.assertRaises(ValueError):
                cat.resolve(bad)

    def test_validate_ids_filters_unknown(self):
        cat = TaskCatalog(self.root)
        self.assertEqual(cat.validate_ids(["taobao_coin", "evil;rm"]),
                         ["taobao_coin"])
        self.assertEqual(cat.validate_ids([]), [])


class TaskRunnerStateMachineTest(unittest.TestCase):
    """运行状态机：success/failed/cancelled，且 exit0 不宣称真实手机任务完成。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.logs = []

    def tearDown(self):
        self._tmp.cleanup()

    def _mk(self, name, body):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def test_exit0_is_success_not_phone_completed(self):
        self._mk("淘宝成就中心签到.py", "print('task ran')\n")
        cat = TaskCatalog(self.root)
        runner = TaskRunner(cat, device_serial="SER123",
                            on_log=self.logs.append)
        results = runner.run_tasks(["taobao_achievement"],
                                   python_exe=sys.executable)
        self.assertEqual(results[0].state, RunState.SUCCESS.value)
        self.assertEqual(results[0].exit_code, 0)
        joined = "\n".join(self.logs)
        # 明示“脚本进程正常结束”，禁止把退出码 0 当真实任务全成功
        self.assertIn("脚本进程正常结束", joined)
        self.assertIn("是否真正在手机上完成任务，请以手机界面为准", joined)

    def test_nonzero_is_failed(self):
        self._mk("闲鱼扔骰子.py", "import sys; sys.exit(3)\n")
        cat = TaskCatalog(self.root)
        runner = TaskRunner(cat, device_serial="S1", on_log=self.logs.append)
        results = runner.run_tasks(["xianyu_dice"], python_exe=sys.executable)
        self.assertEqual(results[0].state, RunState.FAILED.value)
        self.assertEqual(results[0].exit_code, 3)
        joined = "\n".join(self.logs)
        self.assertIn("退出码 3", joined)

    def test_cancelled_stops_process_tree(self):
        self._mk("淘金币任务.py", "import time\nprint('looping', flush=True)\n"
                                  "while True:\n    time.sleep(0.05)\n")
        cat = TaskCatalog(self.root)
        runner = TaskRunner(cat, device_serial="S1", on_log=self.logs.append)
        stop_after = {"done": False}

        def _stop_later():
            time.sleep(0.7)
            runner.stop()

        import threading
        threading.Thread(target=_stop_later, daemon=True).start()
        results = runner.run_tasks(["taobao_coin"], python_exe=sys.executable,
                                   timeout_per_task=15)
        self.assertEqual(results[0].state, RunState.CANCELLED.value)
        self.assertNotEqual(results[0].state, RunState.SUCCESS.value)

    def test_missing_task_gives_failed_skip_note(self):
        cat = TaskCatalog(self.root)
        runner = TaskRunner(cat, device_serial="", on_log=self.logs.append)
        results = runner.run_tasks(["xianyu_dice"], python_exe=sys.executable)
        self.assertEqual(results[0].state, RunState.FAILED.value)
        self.assertIn("跳过", "\n".join(self.logs))

    def test_env_serial_injected(self):
        # 脚本自校验环境变量：与期望不符则退出码非 0（避免依赖日志脱敏规则）
        script = ("import os\n"
                  "import sys\n"
                  "if os.environ.get('COIN11_DEVICE_SERIAL') != 'ABC-123':\n"
                  "    sys.exit(9)\n"
                  "print('serial ok', flush=True)\n")
        self._mk("支付宝农场.py", script)
        cat = TaskCatalog(self.root)
        logs = []
        runner = TaskRunner(cat, device_serial="ABC-123", on_log=logs.append)
        results = runner.run_tasks(["alipay_farm"], python_exe=sys.executable)
        self.assertEqual(results[0].state, RunState.SUCCESS.value,
                         "\n".join(logs))
        # 无 serial 时环境变量不注入 -> 脚本会退出 9
        self._mk("支付宝农场.py", script)
        runner2 = TaskRunner(cat, device_serial="", on_log=logs.append)
        results2 = runner2.run_tasks(["alipay_farm"], python_exe=sys.executable)
        self.assertEqual(results2[0].state, RunState.FAILED.value)

    def test_unavailable_task_failed_state(self):
        """目录里没有任何脚本时，任务不可运行 -> failed（可见失败，不假装成功）。"""
        cat = TaskCatalog(self.root)
        runner = TaskRunner(cat, device_serial="", on_log=self.logs.append)
        results = runner.run_tasks(["tmall_tree"], python_exe=sys.executable)
        self.assertEqual(results[0].state, RunState.FAILED.value)


class LiveLogAndWatchdogTest(unittest.TestCase):
    """0.3.0：实时逐行回调（子进程结束前可见）+ 断线看门狗停止队列。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.logs = []

    def tearDown(self):
        self._tmp.cleanup()

    def _mk(self, name, body):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def test_line_emitted_before_process_exits(self):
        """输出行必须在子进程退出前就到达 on_line（不等退出）。"""
        script = ("import sys, time\n"
                  "print('LIVE-1', flush=True)\n"
                  "sys.stdout.flush()\n"
                  "time.sleep(1.2)\n"
                  "print('LIVE-2', flush=True)\n")
        self._mk("淘宝成就中心签到.py", script)
        cat = TaskCatalog(self.root)
        runner = TaskRunner(cat, device_serial="", on_log=self.logs.append)
        live_seen = threading.Event()
        exit_before_live = {"value": True}

        def _check_while_running():
            # 在子进程仍运行时轮询日志，直到看到 LIVE-1（进程还会 sleep 1.2s）
            deadline = time.time() + 5
            while time.time() < deadline:
                if any("LIVE-1" in l for l in self.logs):
                    live_seen.set()
                    if runner.state == RunState.RUNNING.value:
                        exit_before_live["value"] = False
                    return
                time.sleep(0.02)
        t = threading.Thread(target=_check_while_running, daemon=True)
        t.start()
        results = runner.run_tasks(["taobao_achievement"],
                                   python_exe=sys.executable)
        t.join(timeout=3)
        self.assertEqual(results[0].state, RunState.SUCCESS.value)
        self.assertTrue(live_seen.is_set(),
                        "子进程退出前未看到实时输出行")
        self.assertFalse(exit_before_live["value"],
                         "首行输出直到退出后才可见（说明没有实时转发）")

    def test_watchdog_stops_task_and_skips_queue_on_device_loss(self):
        """断线看门狗：设备连续缺失 -> 停止当前任务并不执行后续任务。"""
        # 第一任务 sleep 3s（期间看门狗连续 miss 2 次会停掉它）
        self._mk("淘金币任务.py",
                 "import time\nprint('T1 started', flush=True)\n"
                 "time.sleep(3)\nprint('T1 done')\n")
        # 第二任务若被执行会写文件（模拟不应发生的后续任务）
        marker = os.path.join(self.root, "SECOND_RAN.txt")
        self._mk("闲鱼扔骰子.py",
                 "import pathlib\n"
                 f"pathlib.Path({marker!r}).write_text('ran', encoding='utf-8')\n")
        cat = TaskCatalog(self.root)

        def fake_watch(_serial):
            return False  # 模拟设备已断开（持续不可用）
        runner = TaskRunner(cat, device_serial="DEV123",
                            on_log=self.logs.append,
                            device_watch=fake_watch,
                            watch_interval=0.05, watch_miss_limit=2)
        results = runner.run_tasks(["taobao_coin", "xianyu_dice"],
                                   python_exe=sys.executable,
                                   timeout_per_task=20)
        joined = "\n".join(self.logs)
        # 第一条被自动停止，原因 = 设备断开
        self.assertEqual(results[0].state, RunState.CANCELLED.value, joined)
        self.assertEqual(results[0].note, "设备已断开，已自动停止任务")
        # 后续任务未运行 -> skipped，且第二脚本文件未被创建
        self.assertEqual(results[1].state, RunState.SKIPPED.value, joined)
        self.assertFalse(os.path.exists(marker), "设备断开后仍执行了后续任务")
        # 日志明确提示“设备已断开，已自动停止任务”
        self.assertIn("设备已断开", joined)
        self.assertIn("自动停止任务", joined)

    def test_watchdog_healthy_device_does_not_stop(self):
        """设备保持可用时任务正常完成，不误报断开。"""
        self._mk("淘宝成就中心签到.py", "print('ok', flush=True)\n")
        cat = TaskCatalog(self.root)
        runner = TaskRunner(cat, device_serial="OK1",
                            on_log=self.logs.append,
                            device_watch=lambda s: True,
                            watch_interval=0.02, watch_miss_limit=2)
        results = runner.run_tasks(["taobao_achievement"],
                                   python_exe=sys.executable)
        self.assertEqual(results[0].state, RunState.SUCCESS.value)
        self.assertNotIn("设备已断开", "\n".join(self.logs))

    def test_manual_stop_marks_cancelled_and_skips_queue(self):
        """手动停止：当前任务 cancelled，后续任务不运行（记为 skipped）。"""
        self._mk("淘金币任务.py",
                 "import time\nwhile True:\n    time.sleep(0.05)\n")
        cat = TaskCatalog(self.root)
        runner = TaskRunner(cat, device_serial="S1", on_log=self.logs.append)
        threading.Thread(target=lambda: (time.sleep(0.4),
                                         runner.stop()), daemon=True).start()
        results = runner.run_tasks(["taobao_coin", "xianyu_dice"],
                                   python_exe=sys.executable,
                                   timeout_per_task=15)
        self.assertEqual(results[0].state, RunState.CANCELLED.value)
        self.assertEqual(results[1].state, RunState.SKIPPED.value)
        self.assertIn("不再运行后续任务", "\n".join(self.logs))


class SafeSubprocessRingBufferTest(unittest.TestCase):
    """SafeSubprocess 最近输出缓冲仍可用于诊断（旧行为不退化）。"""

    def test_recent_output_buffered(self):
        buf = SafeSubprocess.__new__(SafeSubprocess)
        from desktop_app.task_runner import _RingBuffer
        rb = _RingBuffer(max_lines=3)
        rb.append("a\n")
        rb.append("b\n")
        rb.append("c\n")
        rb.append("d\n")
        self.assertEqual(rb.snapshot(), ["b\n", "c\n", "d\n"])  # 只留最近 3
        drained = rb.drain()
        self.assertEqual(drained, ["b\n", "c\n", "d\n"])
        self.assertEqual(rb.snapshot(), [])


if __name__ == "__main__":
    unittest.main()
