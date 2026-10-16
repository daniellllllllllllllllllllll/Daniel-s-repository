"""LLM 端点连通性检测（只探测可达性，不发起任何补全请求、不消耗 token）。

层次：DNS 解析 → TCP 握手 → TLS 握手 → 未鉴权 HTTP 请求。
最后一步故意**不带 API Key**：能收到 401/403 就说明端点可达且接口路径正确，
既证明了连通性，又不会真正调用模型。

不打印任何密钥；只打印主机名与端口。
"""
from __future__ import annotations

import socket
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from config import settings  # noqa: E402

TIMEOUT = 10.0
ATTEMPTS = 3


def probeDns(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        addresses = sorted({info[4][0] for info in infos})
        print(f"  DNS     OK   {host} -> {', '.join(addresses)}")
        return addresses
    except Exception as exc:  # noqa: BLE001
        print(f"  DNS     FAIL {host}: {type(exc).__name__}: {exc}")
        return []


def probeTcp(host: str, port: int) -> bool:
    for attempt in range(1, ATTEMPTS + 1):
        start = time.time()
        try:
            with socket.create_connection((host, port), timeout=TIMEOUT):
                print(
                    f"  TCP     OK   {host}:{port}  "
                    f"第 {attempt} 次尝试 {time.time() - start:.2f}s"
                )
                return True
        except Exception as exc:  # noqa: BLE001
            print(
                f"  TCP     FAIL {host}:{port}  "
                f"第 {attempt}/{ATTEMPTS} 次：{type(exc).__name__}: {exc}  "
                f"{time.time() - start:.2f}s"
            )
    return False


def probeTls(host: str, port: int) -> bool:
    start = time.time()
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                subject = dict(item for rdn in cert.get("subject", ()) for item in rdn)
                print(
                    f"  TLS     OK   {tls.version()}  证书主体 CN={subject.get('commonName', '?')}  "
                    f"{time.time() - start:.2f}s"
                )
                return True
    except Exception as exc:  # noqa: BLE001
        print(f"  TLS     FAIL {host}:{port}: {type(exc).__name__}: {exc}")
        return False


def probeHttp(baseUrl: str) -> None:
    """未鉴权请求：401/403 = 可达且需鉴权；其他状态码也说明 HTTP 层通了。"""
    import requests

    target = baseUrl.rstrip("/") + "/models"
    try:
        response = requests.get(target, timeout=TIMEOUT)
        print(
            f"  HTTP    OK   GET {target} -> {response.status_code} "
            f"（未带密钥；401/403 属预期，仅证明可达）"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  HTTP    FAIL GET {target}: {type(exc).__name__}: {exc}")


def main() -> int:
    baseUrl = settings.LLM_BASE_URL
    print("=== LLM 端点连通性检测（不含任何补全请求）===")
    print(f"provider = {settings.LLM_PROVIDER}")
    print(f"model    = {settings.LLM_MODEL}")
    print(f"api_key  = {'已配置（长度 %d，内容不打印）' % len(settings.LLM_API_KEY) if settings.LLM_API_KEY else '未配置'}")

    if not baseUrl:
        print("  LLM_BASE_URL 为空，无法检测端点。")
        return 2

    parsed = urlparse(baseUrl)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    print(f"endpoint = {parsed.scheme}://{host}:{port}{parsed.path}")
    print()

    addresses = probeDns(host)
    if not addresses:
        print("\n结论：DNS 解析失败，端点不可达。")
        return 1

    tcpOk = probeTcp(host, port)
    if not tcpOk:
        print("\n结论：DNS 正常但 TCP 连不上 → 网络层被阻断或端点未放行。")
        return 1

    tlsOk = probeTls(host, port)
    probeHttp(baseUrl)
    print()
    if tlsOk:
        print("结论：端点网络层可达（DNS/TCP/TLS 均通过）。")
    else:
        print("结论：TCP 通但 TLS 失败。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
