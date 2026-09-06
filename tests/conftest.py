import os
import sys

# 允许直接 `python -m pytest tests/` 或 `python tests/run_tests.py`
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
