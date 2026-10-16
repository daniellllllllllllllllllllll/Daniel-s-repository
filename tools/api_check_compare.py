"""对照探测：区分「整体断网」与「仅该端点不可达」。只做 TCP/TLS 探测。"""
from __future__ import annotations

import socket
import ssl
import sys
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

TARGETS = [
    ("pypi.org", 443, "对照：本次会话已证实可用"),
    ("pypi.tuna.tsinghua.edu.cn", 443, "对照：国内镜像"),
    ("www.baidu.com", 443, "对照：国内站点"),
    ("www.computinger.com", 443, "目标端点 HTTPS"),
    ("www.computinger.com", 80, "目标端点 HTTP"),
    ("170.106.154.30", 443, "目标端点直连 IP"),
    ("170.106.154.30", 80, "目标端点直连 IP (HTTP)"),
]
TIMEOUT = 8.0


def probeTcp(host: str, port: int) -> tuple[bool, str]:
    start = time.time()
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return True, f"{time.time() - start:.2f}s"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc} ({time.time() - start:.2f}s)"


def probeTls(host: str, port: int) -> tuple[bool, str]:
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                return True, str(tls.version())
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


print("=== 对照探测（TCP/TLS，仅探测不发请求）===\n")
for host, port, note in TARGETS:
    ok, detail = probeTcp(host, port)
    tlsText = ""
    if ok and port == 443:
        tlsOk, tlsDetail = probeTls(host, port)
        tlsText = f" | TLS {'OK ' + tlsDetail if tlsOk else 'FAIL ' + tlsDetail}"
    mark = "OK  " if ok else "FAIL"
    print(f"{mark} {host}:{port:<4} {detail}{tlsText}   [{note}]")
