# -*- coding: utf-8 -*-
"""workerChecks: 便捷测试入口（python tests/run_tests.py，等价 unittest discover）。

可用：python -m unittest discover -s tests -t .
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.__main__ import _run_suite  # noqa: E402

if __name__ == "__main__":
    sys.exit(_run_suite())
