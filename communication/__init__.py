"""BlkMaC 网络通信包：长度前缀分帧 + RSA/AES 加密的 TCP 安全信道。"""

from communication.tcp_socket import (
    CommError,
    KeyRegistry,
    MsgType,
    ProtocolError,
    SecureClient,
    SecureMessage,
    SecureServer,
    TamperDetected,
    TransportError,
    UnknownPeer,
    pick_free_port,
    recv_frame,
    send_frame,
)

__all__ = [
    "CommError",
    "KeyRegistry",
    "MsgType",
    "ProtocolError",
    "SecureClient",
    "SecureMessage",
    "SecureServer",
    "TamperDetected",
    "TransportError",
    "UnknownPeer",
    "pick_free_port",
    "recv_frame",
    "send_frame",
]
