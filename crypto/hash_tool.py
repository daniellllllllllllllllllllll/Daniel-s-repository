"""哈希计算工具 —— 上链存证与防篡改校验的基础。

核心约束：**任何要上链的数据，摘要必须经由 :func:`canonical_json` 规范化后再哈希。**
Python 字典是无序的，同一条消息两次 ``json.dumps`` 得到的字符串可能键序不同，
哈希自然不同，链上校验就会出现"数据没被改却验不过"的假阳性。
本模块把规范化这一步固化下来，是整个防篡改链路的信任根。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Sequence

__all__ = [
    "DIGEST_SIZE",
    "to_bytes",
    "canonical_json",
    "sha256_bytes",
    "sha256_hex",
    "message_digest",
    "digest_bytes32",
    "verify_digest",
    "merkle_root",
    "short",
]

# SHA-256 输出 32 字节，恰好等于 Solidity 的 bytes32，无需截断或补齐
DIGEST_SIZE = 32

_ENCODING = "utf-8"


def to_bytes(data: Any) -> bytes:
    """把常见输入统一成 bytes。

    支持 bytes / bytearray / memoryview / str，其余类型走 canonical_json 序列化。
    """
    if isinstance(data, bytes):
        return data
    if isinstance(data, (bytearray, memoryview)):
        return bytes(data)
    if isinstance(data, str):
        return data.encode(_ENCODING)
    return canonical_json(data).encode(_ENCODING)


def _json_default(obj: Any) -> Any:
    """让 canonical_json 能处理 bytes / set / 自定义对象，且结果确定。"""
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return {"__bytes__": bytes(obj).hex()}
    if isinstance(obj, (set, frozenset)):
        # 排序保证确定性；元素不可比较时退化为按 repr 排序
        try:
            return sorted(obj)
        except TypeError:
            return sorted(obj, key=repr)
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    raise TypeError(f"canonical_json 无法序列化类型 {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    """规范化 JSON 序列化：键排序、无多余空白、保留非 ASCII 原字符。

    ``ensure_ascii=False`` 配合固定的 utf-8 编码，中文内容不会被转义成 \\uXXXX，
    可读性更好且长度更短；只要编码固定，确定性不受影响。
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def sha256_bytes(data: Any) -> bytes:
    """返回 32 字节的 SHA-256 摘要。"""
    return hashlib.sha256(to_bytes(data)).digest()


def sha256_hex(data: Any) -> str:
    """返回 64 位小写十六进制的 SHA-256 摘要。"""
    return hashlib.sha256(to_bytes(data)).hexdigest()


def message_digest(message: Any) -> str:
    """计算一条交互消息的摘要（十六进制）。链上链下共用同一个函数。"""
    return sha256_hex(canonical_json(message))


def digest_bytes32(message: Any) -> bytes:
    """计算消息摘要并以 32 字节返回，可直接作为合约的 ``bytes32`` 入参。"""
    return sha256_bytes(canonical_json(message))


def verify_digest(message: Any, expected: str | bytes) -> bool:
    """校验消息与摘要是否匹配。

    使用 :func:`hmac.compare_digest` 做常数时间比较，避免按字节短路比较
    泄漏信息（本项目威胁模型下影响有限，但这是零成本的正确做法）。
    """
    actual = message_digest(message)
    if isinstance(expected, (bytes, bytearray, memoryview)):
        expected = bytes(expected).hex()
    return hmac.compare_digest(actual, str(expected).lower())


def merkle_root(digests: Sequence[str]) -> str:
    """由一组十六进制摘要计算 Merkle 根，用于一次性为整轮任务出具单条上链凭证。

    奇数个节点时复制最后一个节点上提（比特币的做法）。空输入返回全零摘要。
    """
    if not digests:
        return "0" * (DIGEST_SIZE * 2)

    layer = [bytes.fromhex(d) for d in digests]
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        layer = [
            hashlib.sha256(layer[i] + layer[i + 1]).digest() for i in range(0, len(layer), 2)
        ]
    return layer[0].hex()


def short(digest: str | bytes, head: int = 8, tail: int = 6) -> str:
    """把长摘要缩写成 ``a1b2c3d4..9f8e7d`` 形式，仅用于日志展示。"""
    h = bytes(digest).hex() if isinstance(digest, (bytes, bytearray, memoryview)) else str(digest)
    if len(h) <= head + tail + 2:
        return h
    return f"{h[:head]}..{h[-tail:]}"
