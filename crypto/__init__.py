"""BlkMaC 密码学工具包：SHA-256 摘要 + RSA 混合加密 + PSS 数字签名。

注意：本包目录名为小写 ``crypto``，与 pycryptodome 的顶层包 ``Crypto`` 仅大小写不同。
Python 在 Windows 上导入时会做文件名大小写校验，两者不会互相遮蔽；
但如果设置了环境变量 ``PYTHONCASEOK``，校验会被关闭并导致导入错乱。
"""

from crypto.hash_tool import (
    DIGEST_SIZE,
    canonical_json,
    digest_bytes32,
    merkle_root,
    message_digest,
    sha256_bytes,
    sha256_hex,
    short,
    to_bytes,
    verify_digest,
)
from crypto.rsa_tool import (
    CryptoError,
    DecryptionError,
    SignatureError,
    decrypt,
    encrypt,
    ensure_keypair,
    export_public_pem,
    fingerprint,
    generate_keypair,
    import_public_pem,
    load_private_key,
    load_public_key,
    sign,
    verify,
)

__all__ = [
    # hash_tool
    "DIGEST_SIZE",
    "canonical_json",
    "digest_bytes32",
    "merkle_root",
    "message_digest",
    "sha256_bytes",
    "sha256_hex",
    "short",
    "to_bytes",
    "verify_digest",
    # rsa_tool
    "CryptoError",
    "DecryptionError",
    "SignatureError",
    "decrypt",
    "encrypt",
    "ensure_keypair",
    "export_public_pem",
    "fingerprint",
    "generate_keypair",
    "import_public_pem",
    "load_private_key",
    "load_public_key",
    "sign",
    "verify",
]
