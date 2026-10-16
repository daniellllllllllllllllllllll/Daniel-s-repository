"""本地 CONNECT 中继：修正被污染的 DNS 解析，让 git 能连上 github.com。

背景：本机 DNS 把 ``github.com`` 解析到 ``20.27.177.113``（不可达），而 GitHub 真实 IP
（140.82.112.3 等）TCP/TLS 完全正常。git 的 HTTPS 传输只认域名、不提供 ``--resolve``，
因此这里起一个**最小 CONNECT 代理**，把目标主机名映射到正确 IP。

安全性：代理只做原始字节转发，TLS 仍是 **git 与 GitHub 端到端**握手（SNI=github.com），
证书校验照常生效，没有中间人解密，也不关闭 sslVerify。

用法：
    venv_clean\\Scripts\\python.exe tools\\github_proxy.py --port 8899
    git -c http.proxy=http://127.0.0.1:8899 push origin main
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading

DEFAULT_OVERRIDES = {
    "github.com": "140.82.112.3",
    "codeload.github.com": "140.82.112.9",
}
BUFFER = 65536


def pipe(source: socket.socket, sink: socket.socket) -> None:
    try:
        while True:
            chunk = source.recv(BUFFER)
            if not chunk:
                break
            sink.sendall(chunk)
    except OSError:
        pass
    finally:
        for sock in (source, sink):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client: socket.socket, overrides: dict[str, str]) -> None:
    try:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = client.recv(4096)
            if not chunk:
                return
            data += chunk
            if len(data) > 32768:
                return
        head = data.split(b"\r\n\r\n", 1)[0]
        requestLine = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = requestLine.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        hostPort = parts[1]
        host, _, rawPort = hostPort.partition(":")
        port = int(rawPort or "443")
        destination = overrides.get(host, host)
        print(f"[proxy] CONNECT {host}:{port} -> {destination}:{port}", flush=True)
        try:
            upstream = socket.create_connection((destination, port), timeout=20)
        except OSError as exc:
            print(f"[proxy] 上游连接失败：{exc}", flush=True)
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
        pipe(upstream, client)
    except OSError:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="CONNECT 中继，修正 github.com 解析")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="HOST=IP",
        help="额外的域名→IP 映射，可重复",
    )
    args = parser.parse_args()

    overrides = dict(DEFAULT_OVERRIDES)
    for item in args.map:
        name, _, address = item.partition("=")
        if name and address:
            overrides[name.strip()] = address.strip()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(16)
    print(f"[proxy] 监听 {args.host}:{args.port}；映射 {overrides}", flush=True)
    try:
        while True:
            client, _ = server.accept()
            threading.Thread(target=handle, args=(client, overrides), daemon=True).start()
    except KeyboardInterrupt:
        print("[proxy] 退出", flush=True)
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
