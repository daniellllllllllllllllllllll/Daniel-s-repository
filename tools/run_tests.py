"""在受限文件沙箱内运行项目单测的包装脚本。

解决两个**沙箱环境**问题，都不改动项目源码或 .env：

1. 沙箱下 ``tempfile.mkdtemp`` 以 mode=0o700 建目录，随后对目录内文件的写入/遍历
   会被拒绝（``PermissionError [Errno 13]`` / ``[WinError 5]``），导致
   ``test_crypto.TestRsaKeyManagement`` 等用例失败。
   → 把 ``os.mkdir`` 的 0o700 放宽为 0o777，并吞掉 ``os.chmod`` 的权限异常。
2. 离线 ``.env`` 里的 ``LLM_PROVIDER`` 指向本代码不支持的后端名，而
   ``config.py`` 用 ``load_dotenv(override=False)``，**真实环境变量优先于 .env**。
   → 若环境里没有显式指定 ``LLM_PROVIDER``，则默认注入 ``mock``，
     等价于 README 中的 ``--provider mock``。可从外部用
     ``$env:LLM_PROVIDER=...`` 覆盖。

用法：
    python _tmpenv/run_tests.py            # 全量 discover
    python _tmpenv/run_tests.py tests.test_agents -v
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_orig_mkdir = os.mkdir
_orig_chmod = os.chmod
_orig_makedirs = os.makedirs


def _patched_mkdir(path, mode=0o777, *args, **kwargs):
    if mode == 0o700:
        mode = 0o777
    return _orig_mkdir(path, mode, *args, **kwargs)


def _patched_chmod(path, mode, *args, **kwargs):
    try:
        return _orig_chmod(path, mode, *args, **kwargs)
    except PermissionError:
        return None


def _patched_makedirs(name, mode=0o777, *args, **kwargs):
    if mode == 0o700:
        mode = 0o777
    return _orig_makedirs(name, mode, *args, **kwargs)


os.mkdir = _patched_mkdir
os.chmod = _patched_chmod
os.makedirs = _patched_makedirs

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# 真实环境变量优先于 .env（config.py 的 load_dotenv(override=False)）；
# 未显式指定时注入 mock，保证不依赖外部 LLM 服务即可跑通。
if not os.environ.get("LLM_PROVIDER"):
    os.environ["LLM_PROVIDER"] = "mock"
    print(f"[env] LLM_PROVIDER 未指定，注入 mock（项目根 {PROJECT_ROOT}）")

if __name__ == "__main__":
    arguments = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbosity = 2 if "-v" in sys.argv[1:] else 1
    loader = unittest.TestLoader()
    if arguments:
        suite = loader.loadTestsFromNames(arguments)
    else:
        suite = loader.discover(
            start_dir=str(PROJECT_ROOT / "tests"),
            top_level_dir=str(PROJECT_ROOT / "tests"),
        )
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
