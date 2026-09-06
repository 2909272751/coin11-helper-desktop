# -*- coding: utf-8 -*-
"""workerChecks: 单元测试（unittest 风格，零第三方依赖，标准库可运行）。"""
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _run_suite():
    loader = unittest.TestLoader()
    start = os.path.dirname(os.path.abspath(__file__))
    suite = loader.discover(start, pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_suite())
