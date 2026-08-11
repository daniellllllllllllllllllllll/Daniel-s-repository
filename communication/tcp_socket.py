"""加密 TCP 通信模块 —— Agent 之间的安全信道。

线路格式
--------
每一帧 = ``4 字节大端长度前缀 + JSON 信封``::

    +--------+------------------------------+
    | len(4) |  {"msg_id":..., "cipher":...} |
    +--------+------------------------------+

**为什么必须做长度前缀分帧：** TCP 是字节流，没有消息边界。直接
``sock.recv(4096)`` 在连发多条消息时会把两条消息粘在一个包里（粘包），
或把一条长消息拆成多个包（拆包），JSON 解析必然失败。长度前缀是最简单可靠的解法。

安全链路（发送方 → 接收方）
--------------------------
1. 发送方对 ``canonical_json(payload)`` 计算 SHA-256 摘要
2. 用**自己的私钥**对 payload 做 PSS 签名
3. 用**对方的公钥**做 RSA-OAEP + AES-256-GCM 混合加密
4. 接收方解密 → 验签 → 校验摘要，任一步失败即抛
   :class:`TamperDetected`

信封头部（msg_id / sender / receiver）保持明文，因为要用于路由；
真正的业务内容全在 ``cipher`` 字段里。
"""

from __future__ import annotations

import base64
import hmac
import json
import socket
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from config import get_logger, settings
from crypto import hash_tool as ht
from crypto import rsa_tool as rt

logger = get_logger("blkmac.tcp")

__all__ = [
    "CommError",
    "TransportError",
    "ProtocolError",
    "TamperDetected",
    "UnknownPeer",
    "MsgType",
    "SecureMessage",
    "KeyRegistry",
    "SecureServer",
    "SecureClient",
    "send_frame",
    "recv_frame",
    "pick_free_port",
]

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class CommError(Exception):
    """通信层错误基类。"""


class TransportError(CommError):
    """连接失败、超时、对端提前关闭等传输层问题。"""


class ProtocolError(CommError):
    """帧结构或 JSON 信封不符合约定。"""


class TamperDetected(CommError):
    """验签或摘要校验失败——消息在途中被篡改。

    这是论文中"防篡改"要求在传输层的直接证据，出现即应记录并拒绝该消息。
    """


class UnknownPeer(CommError):
    """本地密钥登记表里没有对端的公钥，无法加密或验签。"""


# ---------------------------------------------------------------------------
# 协议常量
# ---------------------------------------------------------------------------

_LEN_FMT = ">I"  # 4 字节大端无符号整数
_LEN_SIZE = struct.calcsize(_LEN_FMT)
_ENCODING = "utf-8"


class MsgType:
    """消息类型常量。"""

    HANDSHAKE = "handshake"  # 明文交换公钥，唯一不加密的类型
    TASK = "task"  # Controller 下发子任务
    RESULT = "result"  # Agent 回传结果
    ACK = "ack"  # 通用回执
    ERROR = "error"  # 处理失败


@dataclass
class SecureMessage:
    """一条已解密并通过校验的消息。"""

    msg_id: str
    msg_type: str
    sender: str
    receiver: str
    payload: Any
    digest: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "msg_type": self.msg_type,
            "sender": self.sender,
            "receiver": self.receiver,
            "payload": self.payload,
            "digest": self.digest,
            "timestamp": self.timestamp,
        }

    def ledger_record(self) -> dict:
        """上链存证用的精简记录（不含明文 payload，只留摘要）。"""
        return {
            "msg_id": self.msg_id,
            "msg_type": self.msg_type,
            "sender": self.sender,
            "receiver": self.receiver,
            "digest": self.digest,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# 分帧读写
# ---------------------------------------------------------------------------


def send_frame(sock: socket.socket, data: bytes) -> None:
    """发送一帧：4 字节长度前缀 + 内容。"""
    if len(data) > 0xFFFFFFFF:
        raise ProtocolError("单帧超过 4GiB 上限")
    try:
        sock.sendall(struct.pack(_LEN_FMT, len(data)) + data)
    except OSError as exc:
        raise TransportError(f"发送失败：{exc}") from exc


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """精确读取 n 字节；对端关闭则返回已读部分（长度不足由调用方判断）。"""
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        try:
            chunk = sock.recv(min(remaining, 65536))
        except socket.timeout as exc:
            raise TransportError(f"读取超时，还差 {remaining} 字节") from exc
        except OSError as exc:
            raise TransportError(f"读取失败：{exc}") from exc
        if not chunk:  # 对端关闭
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(sock: socket.socket, max_frame: int | None = None) -> bytes | None:
    """读取一帧；对端正常关闭返回 ``None``。"""
    max_frame = max_frame if max_frame is not None else settings.TCP_MAX_FRAME

    header = _recv_exact(sock, _LEN_SIZE)
    if not header:
        return None  # 干净的 EOF
    if len(header) < _LEN_SIZE:
        raise ProtocolError("长度前缀不完整，连接被中途截断")

    (length,) = struct.unpack(_LEN_FMT, header)
    if length > max_frame:
        # 不预分配超大缓冲，直接拒绝，防止被单个恶意帧撑爆内存
        raise ProtocolError(f"帧长 {length} 超过上限 {max_frame}")

    body = _recv_exact(sock, length)
    if len(body) < length:
        raise ProtocolError(f"帧正文不完整（期望 {length} 字节，实收 {len(body)}）")
    return body


def pick_free_port(host: str = "127.0.0.1") -> int:
    """让操作系统分配一个空闲端口，测试里用来避免端口冲突。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# 公钥登记表
# ---------------------------------------------------------------------------


class KeyRegistry:
    """线程安全的 ``节点ID -> 公钥`` 登记表。

    私有联盟场景下，各节点的公钥在启动时互相登记；
    也支持通过 :meth:`SecureClient.handshake` 在线获取。
    """

    def __init__(self) -> None:
        self._keys: dict[str, Any] = {}
        self._lock = threading.RLock()

    def register(self, node_id: str, public_key: Any) -> None:
        """登记公钥，接受 RsaKey 对象或 PEM 字符串。"""
        if isinstance(public_key, (str, bytes)):
            public_key = rt.import_public_pem(public_key)
        with self._lock:
            self._keys[node_id] = public_key.publickey()

    def get(self, node_id: str) -> Any:
        with self._lock:
            key = self._keys.get(node_id)
        if key is None:
            raise UnknownPeer(f"未登记节点 {node_id!r} 的公钥，无法加密/验签")
        return key

    def has(self, node_id: str) -> bool:
        with self._lock:
            return node_id in self._keys

    def node_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._keys)

    def __len__(self) -> int:
        with self._lock:
            return len(self._keys)


# ---------------------------------------------------------------------------
# 信封封装 / 拆解
# ---------------------------------------------------------------------------


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(text: str) -> bytes:
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except Exception as exc:
        raise ProtocolError(f"base64 字段解码失败：{exc}") from exc


def _build_envelope(
    *,
    msg_type: str,
    sender: str,
    receiver: str,
    payload: Any,
    private_key: Any,
    peer_public_key: Any,
    msg_id: str | None = None,
) -> dict:
    """签名 + 加密，产出待发送的 JSON 信封。"""
    msg_id = msg_id or uuid.uuid4().hex
    canonical = ht.canonical_json(payload)

    return {
        "v": 1,
        "msg_id": msg_id,
        "msg_type": msg_type,
        "sender": sender,
        "receiver": receiver,
        "timestamp": time.time(),
        "digest": ht.sha256_hex(canonical),
        "signature": _b64e(rt.sign(private_key, canonical)),
        "cipher": _b64e(rt.encrypt(peer_public_key, canonical)),
    }


def _open_envelope(envelope: dict, private_key: Any, registry: KeyRegistry) -> SecureMessage:
    """解密 → 验签 → 校验摘要。任一步失败抛 :class:`TamperDetected`。"""
    for key in ("msg_id", "msg_type", "sender", "receiver", "digest", "signature", "cipher"):
        if key not in envelope:
            raise ProtocolError(f"信封缺少必需字段 {key!r}")

    sender = envelope["sender"]

    # 1) 解密。GCM 认证标签在这里就能挡住绝大多数密文篡改。
    plaintext = rt.decrypt(private_key, _b64d(envelope["cipher"]))
    canonical = plaintext.decode(_ENCODING)

    # 2) 验签：确认确实来自声称的发送方
    sender_key = registry.get(sender)
    if not rt.verify(sender_key, canonical, _b64d(envelope["signature"])):
        raise TamperDetected(f"来自 {sender!r} 的消息签名验证失败")

    # 3) 校验摘要：确认信封头声明的摘要与正文一致。
    #    这里对 canonical 字符串直接哈希（发送方也是这么算的），
    #    不能用 verify_digest——那个函数会再套一层 canonical_json。
    if not hmac.compare_digest(ht.sha256_hex(canonical), str(envelope["digest"]).lower()):
        raise TamperDetected(f"消息 {envelope['msg_id']} 摘要不匹配，内容已被篡改")

    try:
        payload = json.loads(canonical)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"正文不是合法 JSON：{exc}") from exc

    return SecureMessage(
        msg_id=envelope["msg_id"],
        msg_type=envelope["msg_type"],
        sender=sender,
        receiver=envelope["receiver"],
        payload=payload,
        digest=envelope["digest"],
        timestamp=float(envelope.get("timestamp", time.time())),
    )


# ---------------------------------------------------------------------------
# 服务端
# ---------------------------------------------------------------------------

Handler = Callable[[SecureMessage], Any]


class SecureServer:
    """后台线程监听的加密 TCP 服务端。

    每个连接由独立线程处理，循环读取多帧直到对端关闭——
    这样才能覆盖"一个连接连发多条消息"的粘包场景。
    """

    def __init__(
        self,
        node_id: str,
        private_key: Any,
        registry: KeyRegistry,
        handler: Handler | None = None,
        host: str | None = None,
        port: int = 0,
        backlog: int = 32,
    ) -> None:
        self.node_id = node_id
        self.private_key = private_key
        self.public_key = private_key.publickey()
        self.registry = registry
        self.handler = handler
        self.host = host or settings.TCP_HOST
        self._requested_port = port
        self.backlog = backlog

        self._sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._conn_threads: list[threading.Thread] = []
        self._running = threading.Event()
        self._lock = threading.RLock()

        self.received: list[SecureMessage] = []
        self.tamper_events: list[dict] = []

    # -- 生命周期 ---------------------------------------------------------

    @property
    def port(self) -> int:
        """实际监听端口（port=0 时由系统分配，启动后才确定）。"""
        if self._sock is None:
            return self._requested_port
        return self._sock.getsockname()[1]

    @property
    def address(self) -> tuple[str, int]:
        return (self.host, self.port)

    def start(self) -> "SecureServer":
        """启动监听。重复调用是幂等的。"""
        with self._lock:
            if self._running.is_set():
                return self

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Windows 与 Unix 的 SO_REUSEADDR 语义不同：Unix 上它只是允许复用
            # TIME_WAIT 状态的端口，Windows 上却允许后来的 socket 直接抢占
            # 一个正在被监听的端口（端口劫持）。因此 Windows 用
            # SO_EXCLUSIVEADDRUSE 独占端口，其它平台用 SO_REUSEADDR。
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # 仅 Windows 有
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((self.host, self._requested_port))
            except OSError as exc:
                sock.close()
                raise TransportError(
                    f"节点 {self.node_id} 绑定 {self.host}:{self._requested_port} 失败：{exc}"
                ) from exc
            sock.listen(self.backlog)
            sock.settimeout(0.3)  # 让 accept 可被 stop() 打断
            self._sock = sock
            self._running.set()

            self._accept_thread = threading.Thread(
                target=self._accept_loop, name=f"srv-{self.node_id}", daemon=True
            )
            self._accept_thread.start()
            logger.info("节点 %s 已监听 %s:%d", self.node_id, self.host, self.port)
            return self

    def stop(self, timeout: float = 3.0) -> None:
        """停止监听并回收线程。重复调用是幂等的。"""
        with self._lock:
            if not self._running.is_set():
                return
            self._running.clear()

        if self._accept_thread is not None:
            self._accept_thread.join(timeout=timeout)
            self._accept_thread = None

        for t in list(self._conn_threads):
            t.join(timeout=0.5)
        self._conn_threads.clear()

        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        logger.info("节点 %s 已停止监听", self.node_id)

    def __enter__(self) -> "SecureServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # -- 内部循环 ---------------------------------------------------------

    def _accept_loop(self) -> None:
        while self._running.is_set():
            try:
                conn, addr = self._sock.accept()  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                break  # socket 已被 stop() 关闭

            conn.settimeout(settings.TCP_TIMEOUT)
            t = threading.Thread(
                target=self._handle_conn, args=(conn, addr), name=f"conn-{addr[1]}", daemon=True
            )
            t.start()
            self._conn_threads.append(t)
            # 清理已结束的线程句柄，避免长跑时列表无限增长
            self._conn_threads = [x for x in self._conn_threads if x.is_alive()]

    def _handle_conn(self, conn: socket.socket, addr) -> None:
        with conn:
            while self._running.is_set():
                try:
                    raw = recv_frame(conn)
                except (TransportError, ProtocolError) as exc:
                    logger.warning("节点 %s 收帧异常 %s：%s", self.node_id, addr, exc)
                    return
                if raw is None:
                    return  # 对端正常关闭

                try:
                    response = self._process(raw)
                except (TamperDetected, ProtocolError, UnknownPeer, rt.DecryptionError) as exc:
                    # 预期内的安全/协议拒绝：_process 已记过日志，这里不再打完整栈，
                    # 否则篡改检测一触发就刷屏，真正的 bug 反而被淹没
                    response = {"v": 1, "msg_type": MsgType.ERROR, "error": str(exc)}
                except Exception as exc:  # 兜底：任何异常都回错误帧，不让线程静默死掉
                    logger.exception("节点 %s 处理消息时发生未预期错误", self.node_id)
                    response = {"v": 1, "msg_type": MsgType.ERROR, "error": str(exc)}

                try:
                    send_frame(conn, json.dumps(response, ensure_ascii=False).encode(_ENCODING))
                except TransportError:
                    return

    def _process(self, raw: bytes) -> dict:
        try:
            envelope = json.loads(raw.decode(_ENCODING))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"信封不是合法 JSON：{exc}") from exc

        # 握手是唯一的明文消息：交换公钥，为后续加密通信做准备
        if envelope.get("msg_type") == MsgType.HANDSHAKE:
            peer_id = envelope.get("sender", "")
            peer_pem = envelope.get("public_key", "")
            if peer_id and peer_pem:
                self.registry.register(peer_id, peer_pem)
            return {
                "v": 1,
                "msg_type": MsgType.HANDSHAKE,
                "sender": self.node_id,
                "public_key": rt.export_public_pem(self.public_key),
            }

        try:
            message = _open_envelope(envelope, self.private_key, self.registry)
        except (TamperDetected, rt.DecryptionError) as exc:
            self.tamper_events.append(
                {
                    "msg_id": envelope.get("msg_id"),
                    "sender": envelope.get("sender"),
                    "reason": str(exc),
                    "timestamp": time.time(),
                }
            )
            logger.error("节点 %s 检测到篡改：%s", self.node_id, exc)
            raise TamperDetected(str(exc)) from exc

        self.received.append(message)

        result = self.handler(message) if self.handler is not None else {"status": "ok"}

        # 回执同样走签名 + 加密
        return _build_envelope(
            msg_type=MsgType.RESULT,
            sender=self.node_id,
            receiver=message.sender,
            payload=result,
            private_key=self.private_key,
            peer_public_key=self.registry.get(message.sender),
            msg_id=message.msg_id,  # 复用 msg_id 便于请求-响应配对
        )


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class SecureClient:
    """加密 TCP 客户端。

    ``send()`` 是同步请求-响应：发一帧、等一帧。
    """

    def __init__(
        self,
        node_id: str,
        private_key: Any,
        registry: KeyRegistry,
        timeout: float | None = None,
    ) -> None:
        self.node_id = node_id
        self.private_key = private_key
        self.public_key = private_key.publickey()
        self.registry = registry
        self.timeout = timeout if timeout is not None else settings.TCP_TIMEOUT

    def _connect(self, host: str, port: int) -> socket.socket:
        try:
            sock = socket.create_connection((host, port), timeout=self.timeout)
        except OSError as exc:
            raise TransportError(f"连接 {host}:{port} 失败：{exc}") from exc
        sock.settimeout(self.timeout)
        return sock

    def handshake(self, host: str, port: int) -> str:
        """与对端明文交换公钥，返回对端 node_id 并自动登记其公钥。"""
        request = {
            "v": 1,
            "msg_type": MsgType.HANDSHAKE,
            "sender": self.node_id,
            "public_key": rt.export_public_pem(self.public_key),
        }
        with self._connect(host, port) as sock:
            send_frame(sock, json.dumps(request, ensure_ascii=False).encode(_ENCODING))
            raw = recv_frame(sock)
        if raw is None:
            raise TransportError("握手时对端未返回数据")

        reply = json.loads(raw.decode(_ENCODING))
        peer_id = reply.get("sender", "")
        peer_pem = reply.get("public_key", "")
        if not peer_id or not peer_pem:
            raise ProtocolError(f"握手响应不完整：{reply}")
        self.registry.register(peer_id, peer_pem)
        return peer_id

    def send(
        self,
        host: str,
        port: int,
        receiver: str,
        payload: Any,
        msg_type: str = MsgType.TASK,
    ) -> SecureMessage:
        """发送一条加密消息并返回解密校验后的响应。"""
        envelope = _build_envelope(
            msg_type=msg_type,
            sender=self.node_id,
            receiver=receiver,
            payload=payload,
            private_key=self.private_key,
            peer_public_key=self.registry.get(receiver),
        )
        with self._connect(host, port) as sock:
            send_frame(sock, json.dumps(envelope, ensure_ascii=False).encode(_ENCODING))
            raw = recv_frame(sock)

        if raw is None:
            raise TransportError(f"{receiver} 未返回响应")

        reply = json.loads(raw.decode(_ENCODING))
        if reply.get("msg_type") == MsgType.ERROR:
            raise CommError(f"{receiver} 处理失败：{reply.get('error')}")
        return _open_envelope(reply, self.private_key, self.registry)

    def send_many(
        self,
        host: str,
        port: int,
        receiver: str,
        payloads: list[Any],
        msg_type: str = MsgType.TASK,
    ) -> list[SecureMessage]:
        """在**同一个连接**上连发多条消息，用于验证分帧正确处理粘包。"""
        envelopes = [
            _build_envelope(
                msg_type=msg_type,
                sender=self.node_id,
                receiver=receiver,
                payload=p,
                private_key=self.private_key,
                peer_public_key=self.registry.get(receiver),
            )
            for p in payloads
        ]

        replies: list[SecureMessage] = []
        with self._connect(host, port) as sock:
            for env in envelopes:
                send_frame(sock, json.dumps(env, ensure_ascii=False).encode(_ENCODING))
            for _ in envelopes:
                raw = recv_frame(sock)
                if raw is None:
                    raise TransportError("连发消息时对端提前关闭")
                reply = json.loads(raw.decode(_ENCODING))
                if reply.get("msg_type") == MsgType.ERROR:
                    raise CommError(f"{receiver} 处理失败：{reply.get('error')}")
                replies.append(_open_envelope(reply, self.private_key, self.registry))
        return replies
