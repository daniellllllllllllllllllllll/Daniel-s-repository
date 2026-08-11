"""RSA 加解密与数字签名工具。

设计要点
--------
1. **混合加密，而不是裸 RSA。**
   RSA-OAEP 单次可加密的明文上限是 ``密钥字节数 - 2*哈希长度 - 2``——
   2048 位密钥配 SHA-256 只有 190 字节。子任务描述、LLM 回答动辄上千字符，
   裸 RSA 会直接抛 ``ValueError: Plaintext is too long``。
   因此这里用标准做法：随机生成一次性 AES-256 会话密钥加密正文（GCM 模式，
   自带完整性校验），再用 RSA-OAEP 加密该会话密钥。

2. **签名与加密分离。**
   加密保证机密性，签名（PKCS#1 PSS）保证来源真实性与不可否认性。
   两者用途不同，不可互相替代。

3. **密钥文件按需生成。**
   仓库里不存放任何私钥；``ensure_keypair()`` 在私钥缺失时才生成，
   并且 .gitignore 已忽略 ``crypto/*.key``。
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

try:  # pycryptodome 装的是 Crypto 命名空间
    from Crypto.Cipher import AES, PKCS1_OAEP
    from Crypto.Hash import SHA256
    from Crypto.PublicKey import RSA
    from Crypto.PublicKey.RSA import RsaKey
    from Crypto.Random import get_random_bytes
    from Crypto.Signature import pss
except ImportError:  # pragma: no cover - 仅在装了 pycryptodomex 时走这一支
    try:
        from Cryptodome.Cipher import AES, PKCS1_OAEP
        from Cryptodome.Hash import SHA256
        from Cryptodome.PublicKey import RSA
        from Cryptodome.PublicKey.RSA import RsaKey
        from Cryptodome.Random import get_random_bytes
        from Cryptodome.Signature import pss
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "找不到 pycryptodome。请执行 pip install -r requirements.txt。\n"
            "注意：本地包目录名为小写 crypto/，与 pycryptodome 的顶层包 Crypto 只差大小写。"
            "Windows 文件系统大小写不敏感，但 Python 导入时会做大小写校验，正常不冲突；"
            "若设置了环境变量 PYTHONCASEOK 则会冲突，请取消该变量。"
        ) from exc

from config import settings

__all__ = [
    "CryptoError",
    "DecryptionError",
    "SignatureError",
    "generate_keypair",
    "ensure_keypair",
    "load_private_key",
    "load_public_key",
    "export_public_pem",
    "export_private_pem",
    "import_public_pem",
    "import_private_pem",
    "encrypt",
    "decrypt",
    "sign",
    "verify",
    "fingerprint",
]


class CryptoError(Exception):
    """密码学操作失败的基类。"""


class DecryptionError(CryptoError):
    """解密失败：密文损坏、被篡改，或用错了私钥。"""


class SignatureError(CryptoError):
    """签名验证失败。"""


# ---------------------------------------------------------------------------
# 密文信封格式
# ---------------------------------------------------------------------------
#   MAGIC(4) | VERSION(1) | RSA_LEN(2,BE) | RSA_BLOB | NONCE(12) | TAG(16) | CIPHERTEXT
# RSA_BLOB 是被 RSA-OAEP 加密的 32 字节 AES 会话密钥，长度等于 RSA 模长。
_MAGIC = b"BMC1"
_VERSION = 1
_HEADER_FMT = ">4sBH"  # magic, version, rsa_len
_HEADER_LEN = struct.calcsize(_HEADER_FMT)  # 7
_NONCE_LEN = 12  # GCM 推荐长度
_TAG_LEN = 16
_SESSION_KEY_LEN = 32  # AES-256

_DEFAULT_PRIVATE_NAME = "private.key"
_DEFAULT_PUBLIC_NAME = "public.key"


# ---------------------------------------------------------------------------
# 密钥生成与读写
# ---------------------------------------------------------------------------


def generate_keypair(bits: int | None = None) -> tuple[RsaKey, RsaKey]:
    """在内存中生成一对 RSA 密钥，返回 ``(私钥, 公钥)``。

    每个 Task Agent 启动时各自持有一对临时密钥，不落盘。
    """
    bits = bits or settings.RSA_KEY_BITS
    if bits < 2048:
        raise ValueError(f"RSA 密钥长度 {bits} 过短，至少 2048 位")
    private_key = RSA.generate(bits)
    return private_key, private_key.publickey()


def ensure_keypair(
    key_dir: str | Path | None = None,
    bits: int | None = None,
    overwrite: bool = False,
) -> tuple[Path, Path]:
    """确保目录下存在 ``private.key`` / ``public.key``，缺失时生成。

    返回 ``(私钥路径, 公钥路径)``。已存在且 ``overwrite=False`` 时直接复用，
    因此可以安全地重复调用。
    """
    directory = Path(key_dir) if key_dir is not None else settings.RSA_KEY_DIR
    directory.mkdir(parents=True, exist_ok=True)

    private_path = directory / _DEFAULT_PRIVATE_NAME
    public_path = directory / _DEFAULT_PUBLIC_NAME

    if not overwrite and private_path.exists() and public_path.exists():
        return private_path, public_path

    private_key, public_key = generate_keypair(bits)
    private_path.write_bytes(private_key.export_key("PEM"))
    public_path.write_bytes(public_key.export_key("PEM"))

    # 私钥权限收紧到仅当前用户可读写（Windows 上 chmod 语义有限，失败不致命）
    try:
        private_path.chmod(0o600)
    except OSError:
        pass

    return private_path, public_path


def load_private_key(path: str | Path | None = None, passphrase: str | None = None) -> RsaKey:
    """从 PEM 文件加载私钥；文件不存在时抛出带明确指引的异常。"""
    p = Path(path) if path is not None else settings.RSA_KEY_DIR / _DEFAULT_PRIVATE_NAME
    if not p.exists():
        raise FileNotFoundError(
            f"私钥文件不存在：{p}\n请先执行： python -c \"from crypto.rsa_tool import "
            'ensure_keypair; ensure_keypair()"'
        )
    return RSA.import_key(p.read_bytes(), passphrase=passphrase)


def load_public_key(path: str | Path | None = None) -> RsaKey:
    """从 PEM 文件加载公钥。"""
    p = Path(path) if path is not None else settings.RSA_KEY_DIR / _DEFAULT_PUBLIC_NAME
    if not p.exists():
        raise FileNotFoundError(f"公钥文件不存在：{p}")
    return RSA.import_key(p.read_bytes())


def export_public_pem(key: RsaKey) -> str:
    """导出公钥 PEM 字符串，用于 Agent 之间通过 TCP 交换公钥。"""
    return key.publickey().export_key("PEM").decode("ascii")


def export_private_pem(key: RsaKey) -> str:
    """导出私钥 PEM 字符串。仅用于测试，切勿写入日志或上链。"""
    return key.export_key("PEM").decode("ascii")


def import_public_pem(pem: str | bytes) -> RsaKey:
    """从 PEM 字符串导入公钥。"""
    key = RSA.import_key(pem)
    return key.publickey()


def import_private_pem(pem: str | bytes, passphrase: str | None = None) -> RsaKey:
    """从 PEM 字符串导入私钥。"""
    return RSA.import_key(pem, passphrase=passphrase)


def fingerprint(key: RsaKey) -> str:
    """公钥指纹（SHA-256 前 16 位十六进制），用于日志里区分 Agent 身份。"""
    from crypto.hash_tool import sha256_hex

    return sha256_hex(key.publickey().export_key("DER"))[:16]


# ---------------------------------------------------------------------------
# 混合加密
# ---------------------------------------------------------------------------


def encrypt(public_key: RsaKey, plaintext: Any) -> bytes:
    """用接收方公钥加密任意长度的明文，返回自描述的密文信封。

    ``plaintext`` 可以是 bytes / str / 任意可 JSON 化的对象。
    """
    from crypto.hash_tool import to_bytes

    data = to_bytes(plaintext)

    session_key = get_random_bytes(_SESSION_KEY_LEN)

    rsa_cipher = PKCS1_OAEP.new(public_key.publickey(), hashAlgo=SHA256)
    rsa_blob = rsa_cipher.encrypt(session_key)

    aes_cipher = AES.new(session_key, AES.MODE_GCM, nonce=get_random_bytes(_NONCE_LEN))
    ciphertext, tag = aes_cipher.encrypt_and_digest(data)

    header = struct.pack(_HEADER_FMT, _MAGIC, _VERSION, len(rsa_blob))
    return header + rsa_blob + aes_cipher.nonce + tag + ciphertext


def decrypt(private_key: RsaKey, envelope: bytes) -> bytes:
    """用自己的私钥解开密文信封，返回原始明文字节。

    密文被篡改时 GCM 的认证标签校验会失败，这里统一转成
    :class:`DecryptionError`——这正是"防篡改"在传输层的第一道防线。
    """
    if not isinstance(envelope, (bytes, bytearray, memoryview)):
        raise DecryptionError("密文必须是 bytes 类型")
    envelope = bytes(envelope)

    if len(envelope) < _HEADER_LEN:
        raise DecryptionError("密文长度不足，无法解析信封头")

    magic, version, rsa_len = struct.unpack(_HEADER_FMT, envelope[:_HEADER_LEN])
    if magic != _MAGIC:
        raise DecryptionError(f"密文魔数不匹配（期望 {_MAGIC!r}，实际 {magic!r}）")
    if version != _VERSION:
        raise DecryptionError(f"不支持的密文版本 {version}")

    offset = _HEADER_LEN
    min_len = offset + rsa_len + _NONCE_LEN + _TAG_LEN
    if len(envelope) < min_len:
        raise DecryptionError("密文被截断，长度与信封头声明不符")

    rsa_blob = envelope[offset : offset + rsa_len]
    offset += rsa_len
    nonce = envelope[offset : offset + _NONCE_LEN]
    offset += _NONCE_LEN
    tag = envelope[offset : offset + _TAG_LEN]
    offset += _TAG_LEN
    ciphertext = envelope[offset:]

    try:
        rsa_cipher = PKCS1_OAEP.new(private_key, hashAlgo=SHA256)
        session_key = rsa_cipher.decrypt(rsa_blob)
    except Exception as exc:
        raise DecryptionError(f"会话密钥解密失败（私钥不匹配或密文损坏）：{exc}") from exc

    try:
        aes_cipher = AES.new(session_key, AES.MODE_GCM, nonce=nonce)
        return aes_cipher.decrypt_and_verify(ciphertext, tag)
    except Exception as exc:
        raise DecryptionError(f"正文认证失败，密文已被篡改：{exc}") from exc


# ---------------------------------------------------------------------------
# 数字签名
# ---------------------------------------------------------------------------


def sign(private_key: RsaKey, data: Any) -> bytes:
    """用私钥对数据做 PKCS#1 PSS 签名，返回签名字节。"""
    from crypto.hash_tool import to_bytes

    digest = SHA256.new(to_bytes(data))
    return pss.new(private_key).sign(digest)


def verify(public_key: RsaKey, data: Any, signature: bytes) -> bool:
    """验签，返回布尔值而不是抛异常，方便调用方按业务决定如何处理。"""
    from crypto.hash_tool import to_bytes

    if not signature:
        return False
    try:
        digest = SHA256.new(to_bytes(data))
        pss.new(public_key.publickey()).verify(digest, signature)
        return True
    except (ValueError, TypeError):
        return False


if __name__ == "__main__":  # 便捷入口：python -m crypto.rsa_tool
    priv_path, pub_path = ensure_keypair()
    print(f"私钥：{priv_path}")
    print(f"公钥：{pub_path}")
    print(f"指纹：{fingerprint(load_public_key(pub_path))}")
