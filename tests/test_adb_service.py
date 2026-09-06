# -*- coding: utf-8 -*-
"""ADB 输出解析与设备状态映射单元测试。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desktop_app.adb_service import (  # noqa: E402
    AdbError,
    AdbService,
    DeviceInfo,
    parse_devices_l,
    validate_serial,
)


class ParseDevicesLTest(unittest.TestCase):
    def test_parses_devices_l(self):
        out = (
            "List of devices attached\n"
            "R58M1234ABCD\tdevice product:star2lte model:SM_G9650 "
            "device:star2lte transport_id:3\n"
            "emulator-5554\tunauthorized product:sdk_gphone64_x86_64 "
            "model:sdk_gphone64_x86_64 device:emu64x transport_id:1\n"
            "\n"
        )
        devs = parse_devices_l(out)
        self.assertEqual(len(devs), 2)
        self.assertEqual(devs[0].serial, "R58M1234ABCD")
        self.assertTrue(devs[0].is_device)
        self.assertEqual(devs[0].model, "SM_G9650")
        self.assertEqual(devs[0].transport_id, "3")
        self.assertFalse(devs[1].is_device)
        self.assertEqual(devs[1].state, "unauthorized")

    def test_ignores_headers_and_blank(self):
        self.assertEqual(parse_devices_l(""), [])
        self.assertEqual(parse_devices_l("List of devices attached\n"), [])
        self.assertEqual(parse_devices_l("  \n \n"), [])

    def test_offline_state_mapping(self):
        devs = parse_devices_l("1234abc\toffline\n")
        self.assertEqual(devs[0].state, "offline")
        self.assertFalse(devs[0].is_device)


class SerialValidationTest(unittest.TestCase):
    def test_valid_serial_ok(self):
        self.assertEqual(validate_serial("R58M1234ABCD"), "R58M1234ABCD")
        self.assertEqual(validate_serial("emulator-5554"), "emulator-5554")
        self.assertEqual(validate_serial("192.168.1.5:5555"), "192.168.1.5:5555")

    def test_invalid_serial_rejected(self):
        for bad in ("", "a b", "a;rm -rf /", "a&cmd", "..\\..\\x",
                    "a" * 100, None, 123):
            with self.assertRaises(AdbError):
                validate_serial(bad)


class ConnectionStateMappingTest(unittest.TestCase):
    """设备状态 -> 用户可见提示的映射（无假‘已连接’）。"""

    def _service_with(self, state_outcome):
        svc = AdbService.__new__(AdbService)
        svc.adb_exe = r"C:\fake\platform-tools\adb.exe"
        svc._log = lambda _t: None
        return svc

    def test_no_adb_reports_state(self):
        svc = self._service_with(None)
        svc.available = lambda: False
        info = svc.connection_state()
        self.assertEqual(info["state"], "no_adb")

    def test_no_device(self):
        svc = self._service_with([])
        svc.available = lambda: True
        svc.list_devices = lambda: []
        info = svc.connection_state()
        self.assertEqual(info["state"], "no_device")
        self.assertNotEqual(info["hint"], "")

    def test_unauthorized_maps(self):
        svc = self._service_with(None)
        svc.available = lambda: True
        svc.list_devices = lambda: [DeviceInfo("X", state="unauthorized")]
        info = svc.connection_state()
        self.assertEqual(info["state"], "unauthorized")
        self.assertIn("授权", info["hint"])

    def test_offline_maps(self):
        svc = self._service_with(None)
        svc.available = lambda: True
        svc.list_devices = lambda: [DeviceInfo("X", state="offline")]
        info = svc.connection_state()
        self.assertEqual(info["state"], "offline")

    def test_ready(self):
        svc = self._service_with(None)
        svc.available = lambda: True
        svc.list_devices = lambda: [DeviceInfo("X", state="device")]
        info = svc.connection_state()
        self.assertEqual(info["state"], "device")

    def test_adb_absent_no_fake_connected(self):
        svc = self._service_with(None)
        svc.available = lambda: False
        info = svc.connection_state()
        self.assertEqual(info["state"], "no_adb")
        self.assertEqual(info["devices"], [])


class DeviceReadyProbeTest(unittest.TestCase):
    """0.3.0 看门狗设备探测：单次检查带超时、缺设备/异常按不可用。"""

    def _svc(self):
        svc = AdbService.__new__(AdbService)
        svc.adb_exe = r"C:\fake\platform-tools\adb.exe"
        svc._log = lambda _t: None
        import threading
        svc._lock = threading.Lock()
        return svc

    def test_ready_when_serial_device_present(self):
        svc = self._svc()
        svc.list_devices = lambda **kw: [DeviceInfo("S1", state="device"),
                                         DeviceInfo("S2", state="offline")]
        self.assertTrue(svc.device_ready("S1"))
        self.assertFalse(svc.device_ready("S2"))

    def test_missing_serial_not_ready(self):
        svc = self._svc()
        svc.list_devices = lambda **kw: []
        self.assertFalse(svc.device_ready("S1"))

    def test_error_or_no_adb_not_ready(self):
        svc = self._svc()
        svc.list_devices = lambda **kw: (_ for _ in ()).throw(
            AdbError("模拟 adb 错误"))
        self.assertFalse(svc.device_ready("S1"))
        svc2 = self._svc()
        svc2.available = lambda: False
        self.assertFalse(svc2.device_ready("S1"))

    def test_serial_validation_rejects_bad(self):
        svc = self._svc()
        with self.assertRaises(AdbError):
            svc.device_ready("a;rm -rf /")


if __name__ == "__main__":
    unittest.main()
