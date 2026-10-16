"""网络连通性诊断：对若干候选主机做 TCP/TLS 探测，不发送任何业务请求。"""
import socket
import ssl
import time

HOSTS = [
    "pypi.org",
    "files.pythonhosted.org",
    "pypi.tuna.tsinghua.edu.cn",
    "mirrors.aliyun.com",
    "mirrors.cloud.tencent.com",
]

for host in HOSTS:
    for port in (443,):
        start = time.time()
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=8) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as tls:
                    print(f"{host}:{port} TCP+TLS OK  tls={tls.version()}  {time.time()-start:.2f}s")
        except Exception as exc:  # noqa: BLE001
            print(f"{host}:{port} FAIL  {type(exc).__name__}: {exc}  {time.time()-start:.2f}s")
