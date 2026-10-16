"""环境诊断：列出当前解释器可见/缺失的依赖，以及候选 site-packages 里已装的包。

用途：确认 `venv_clean` 具备运行项目所需的全部依赖
（web3 / pycryptodome / python-dotenv / requests / pandas / matplotlib）。

用法：
    venv_clean\\Scripts\\python.exe tools\\env_check.py
"""
from __future__ import annotations

import importlib.util as u
import os
import site
import sys
from pathlib import Path

MODULES = ["web3", "Crypto", "dotenv", "requests", "pandas", "matplotlib", "numpy"]

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def candidate_dirs() -> list[str]:
    """候选 site-packages：当前解释器的、用户级的、以及同项目下其它虚拟环境的。"""
    dirs: list[str] = []
    for getter in (lambda: site.getsitepackages(), lambda: [site.getusersitepackages()]):
        try:
            dirs.extend(getter())
        except Exception:  # noqa: BLE001
            pass
    for candidate in PROJECT_ROOT.glob("*/venv/Lib/site-packages"):
        dirs.append(str(candidate))
    deduped: list[str] = []
    for directory in dirs:
        if directory not in deduped:
            deduped.append(directory)
    return deduped


print("python:", sys.executable)
print("version:", sys.version.split()[0])
print("ENABLE_USER_SITE:", site.ENABLE_USER_SITE)
print()

print("=== 当前解释器可见模块 ===")
for name in MODULES:
    try:
        spec = u.find_spec(name)
    except Exception as exc:  # noqa: BLE001
        print(f"  {name:12} MISSING ({type(exc).__name__})")
        continue
    print(f"  {name:12} {'OK' if spec else 'MISSING'}")

print()
print("=== 候选目录里的包 ===")
for directory in candidate_dirs():
    print(f"--- {directory}")
    if not os.path.isdir(directory):
        print("    (不存在)")
        continue
    names = sorted(
        n
        for n in os.listdir(directory)
        if not n.endswith(".dist-info") and not n.startswith("__")
    )
    print("   ", ", ".join(names[:40]) if names else "(空)")
