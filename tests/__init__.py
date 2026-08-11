"""BlkMaC 单元测试包（标准库 unittest，不依赖 pytest）。

运行全部测试::

    python -m unittest discover -s tests -v

运行单个模块::

    python -m unittest tests.test_crypto -v
"""

import sys
from pathlib import Path

# 让 `python -m unittest discover -s tests` 在任意工作目录下都能 import 到项目包
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
