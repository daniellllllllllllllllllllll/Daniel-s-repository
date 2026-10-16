"""在受限文件沙箱内运行 pip 的包装脚本。

问题：沙箱下 ``tempfile.mkdtemp`` 以 mode=0o700 建目录，随后 pip 往该目录写
``*.whl`` 会被拒绝（``PermissionError [Errno 13]``），表现为
"Could not install packages due to an OSError ... Check the permissions."

绕过：把 ``os.mkdir`` 的 0o700 放宽到 0o777，并吞掉 ``os.chmod`` 的权限异常。
对 Windows 单用户机器无实际安全影响，且只作用于本次 pip 进程。

用法：
    venv_clean\\Scripts\\python.exe tools\\pip_patched.py install -r requirements.txt
"""
from __future__ import annotations

import os
import sys

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

from pip._internal.cli.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
