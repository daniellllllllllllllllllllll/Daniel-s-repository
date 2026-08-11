"""crypto 模块测试：哈希确定性、RSA 混合加解密、数字签名、防篡改。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crypto import hash_tool as ht
from crypto import rsa_tool as rt


class TestHashTool(unittest.TestCase):
    """哈希与规范化序列化。"""

    def test_sha256_known_vector(self):
        """对标准测试向量，确认用的确实是 SHA-256。"""
        self.assertEqual(
            ht.sha256_hex("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )

    def test_sha256_accepts_str_and_bytes_identically(self):
        self.assertEqual(ht.sha256_hex("hello"), ht.sha256_hex(b"hello"))

    def test_digest_size_is_32_bytes(self):
        """必须正好 32 字节，才能直接喂给 Solidity 的 bytes32。"""
        self.assertEqual(len(ht.sha256_bytes("x")), ht.DIGEST_SIZE)
        self.assertEqual(len(ht.digest_bytes32({"a": 1})), 32)

    def test_canonical_json_is_key_order_independent(self):
        """本项目防篡改的信任根：键序不影响摘要。"""
        a = {"task": "t1", "sender": "agent-1", "payload": {"y": 2, "x": 1}}
        b = {"payload": {"x": 1, "y": 2}, "sender": "agent-1", "task": "t1"}
        self.assertEqual(ht.canonical_json(a), ht.canonical_json(b))
        self.assertEqual(ht.message_digest(a), ht.message_digest(b))

    def test_canonical_json_has_no_whitespace(self):
        self.assertEqual(ht.canonical_json({"b": 1, "a": 2}), '{"a":2,"b":1}')

    def test_canonical_json_keeps_chinese_readable(self):
        """ensure_ascii=False，中文不被转义成 \\uXXXX。"""
        self.assertIn("子任务", ht.canonical_json({"desc": "子任务"}))

    def test_canonical_json_handles_bytes_and_set(self):
        out = ht.canonical_json({"raw": b"\x01\x02", "tags": {"b", "a"}})
        self.assertIn("0102", out)
        self.assertIn('["a","b"]', out)

    def test_different_content_gives_different_digest(self):
        self.assertNotEqual(ht.message_digest({"v": 1}), ht.message_digest({"v": 2}))

    def test_verify_digest_detects_tampering(self):
        """核心防篡改断言：改一个字段，摘要立刻对不上。"""
        msg = {"task_id": "T-01", "content": "3.14 是 pi 的近似值", "sender": "agent-2"}
        digest = ht.message_digest(msg)
        self.assertTrue(ht.verify_digest(msg, digest))

        tampered = dict(msg, content="3.15 是 pi 的近似值")
        self.assertFalse(ht.verify_digest(tampered, digest))

    def test_verify_digest_accepts_bytes_expected(self):
        msg = {"k": "v"}
        self.assertTrue(ht.verify_digest(msg, ht.digest_bytes32(msg)))

    def test_merkle_root_stable_and_sensitive(self):
        leaves = [ht.message_digest({"i": i}) for i in range(5)]  # 奇数，覆盖补齐分支
        root = ht.merkle_root(leaves)
        self.assertEqual(len(root), 64)
        self.assertEqual(root, ht.merkle_root(leaves))  # 确定性

        changed = list(leaves)
        changed[2] = ht.message_digest({"i": 999})
        self.assertNotEqual(root, ht.merkle_root(changed))

    def test_merkle_root_empty(self):
        self.assertEqual(ht.merkle_root([]), "0" * 64)

    def test_short_is_display_only(self):
        d = ht.sha256_hex("x")
        self.assertIn("..", ht.short(d))
        self.assertLess(len(ht.short(d)), len(d))


class TestRsaKeyManagement(unittest.TestCase):
    """密钥生成、落盘与加载。"""

    def test_generate_keypair_default_bits(self):
        priv, pub = rt.generate_keypair(2048)
        self.assertEqual(priv.size_in_bits(), 2048)
        self.assertFalse(pub.has_private())
        self.assertTrue(priv.has_private())

    def test_reject_weak_key_size(self):
        with self.assertRaises(ValueError):
            rt.generate_keypair(1024)

    def test_ensure_keypair_creates_then_reuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            priv_path, pub_path = rt.ensure_keypair(tmp, bits=2048)
            self.assertTrue(Path(priv_path).exists())
            self.assertTrue(Path(pub_path).exists())
            first = Path(priv_path).read_bytes()

            # 再调一次不应覆盖已有密钥（幂等）
            rt.ensure_keypair(tmp, bits=2048)
            self.assertEqual(first, Path(priv_path).read_bytes())

    def test_ensure_keypair_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            priv_path, _ = rt.ensure_keypair(tmp, bits=2048)
            first = Path(priv_path).read_bytes()
            rt.ensure_keypair(tmp, bits=2048, overwrite=True)
            self.assertNotEqual(first, Path(priv_path).read_bytes())

    def test_load_roundtrip_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            priv_path, pub_path = rt.ensure_keypair(tmp, bits=2048)
            priv = rt.load_private_key(priv_path)
            pub = rt.load_public_key(pub_path)
            self.assertEqual(priv.publickey().export_key(), pub.export_key())

    def test_load_missing_private_key_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError) as ctx:
                rt.load_private_key(Path(tmp) / "nope.key")
            self.assertIn("ensure_keypair", str(ctx.exception))

    def test_pem_exchange(self):
        """Agent 之间通过 PEM 文本交换公钥。"""
        priv, pub = rt.generate_keypair(2048)
        pem = rt.export_public_pem(pub)
        self.assertIn("BEGIN PUBLIC KEY", pem)

        restored = rt.import_public_pem(pem)
        self.assertEqual(restored.export_key(), pub.export_key())

    def test_fingerprint_is_stable_and_distinct(self):
        _, pub_a = rt.generate_keypair(2048)
        _, pub_b = rt.generate_keypair(2048)
        self.assertEqual(rt.fingerprint(pub_a), rt.fingerprint(pub_a))
        self.assertNotEqual(rt.fingerprint(pub_a), rt.fingerprint(pub_b))
        self.assertEqual(len(rt.fingerprint(pub_a)), 16)


class TestHybridEncryption(unittest.TestCase):
    """RSA-OAEP + AES-256-GCM 混合加密。"""

    @classmethod
    def setUpClass(cls):
        # 2048 位密钥生成不算快，整个类复用一对
        cls.priv, cls.pub = rt.generate_keypair(2048)
        cls.other_priv, cls.other_pub = rt.generate_keypair(2048)

    def test_roundtrip_short_text(self):
        blob = rt.encrypt(self.pub, "hello 区块链")
        self.assertEqual(rt.decrypt(self.priv, blob).decode(), "hello 区块链")

    def test_roundtrip_long_text_beyond_rsa_limit(self):
        """关键用例：明文远超裸 RSA 的 190 字节上限，混合加密必须照常工作。"""
        plaintext = "多智能体协同任务描述。" * 2000  # 约 2 万字符
        self.assertGreater(len(plaintext.encode()), 190)

        blob = rt.encrypt(self.pub, plaintext)
        self.assertEqual(rt.decrypt(self.priv, blob).decode(), plaintext)

    def test_roundtrip_empty_and_binary(self):
        self.assertEqual(rt.decrypt(self.priv, rt.encrypt(self.pub, b"")), b"")
        raw = bytes(range(256))
        self.assertEqual(rt.decrypt(self.priv, rt.encrypt(self.pub, raw)), raw)

    def test_roundtrip_json_object(self):
        import json

        obj = {"task_id": "T-7", "domain": "math", "difficulty": 3}
        blob = rt.encrypt(self.pub, obj)
        self.assertEqual(json.loads(rt.decrypt(self.priv, blob)), obj)

    def test_ciphertext_is_randomized(self):
        """同一明文两次加密结果必须不同（会话密钥与 nonce 随机）。"""
        a = rt.encrypt(self.pub, "same")
        b = rt.encrypt(self.pub, "same")
        self.assertNotEqual(a, b)
        self.assertEqual(rt.decrypt(self.priv, a), rt.decrypt(self.priv, b))

    def test_wrong_private_key_fails(self):
        blob = rt.encrypt(self.pub, "secret")
        with self.assertRaises(rt.DecryptionError):
            rt.decrypt(self.other_priv, blob)

    def test_tampered_ciphertext_body_detected(self):
        """GCM 认证标签：正文被改一个 bit 就必须解密失败。"""
        blob = bytearray(rt.encrypt(self.pub, "转账金额 100"))
        blob[-1] ^= 0x01
        with self.assertRaises(rt.DecryptionError):
            rt.decrypt(self.priv, bytes(blob))

    def test_tampered_tag_detected(self):
        blob = bytearray(rt.encrypt(self.pub, "payload"))
        # 认证标签位于信封尾部之前，这里改倒数第 20 字节（落在 tag 或密文区）
        blob[-20] ^= 0xFF
        with self.assertRaises(rt.DecryptionError):
            rt.decrypt(self.priv, bytes(blob))

    def test_truncated_ciphertext_detected(self):
        blob = rt.encrypt(self.pub, "payload")
        with self.assertRaises(rt.DecryptionError):
            rt.decrypt(self.priv, blob[:20])

    def test_bad_magic_detected(self):
        blob = bytearray(rt.encrypt(self.pub, "payload"))
        blob[0] = ord("X")
        with self.assertRaises(rt.DecryptionError) as ctx:
            rt.decrypt(self.priv, bytes(blob))
        self.assertIn("魔数", str(ctx.exception))

    def test_non_bytes_input_rejected(self):
        with self.assertRaises(rt.DecryptionError):
            rt.decrypt(self.priv, "not bytes")


class TestSignature(unittest.TestCase):
    """PKCS#1 PSS 数字签名。"""

    @classmethod
    def setUpClass(cls):
        cls.priv, cls.pub = rt.generate_keypair(2048)
        cls.attacker_priv, cls.attacker_pub = rt.generate_keypair(2048)

    def test_sign_verify_roundtrip(self):
        data = b"agent-1 -> controller: subtask done"
        self.assertTrue(rt.verify(self.pub, data, rt.sign(self.priv, data)))

    def test_sign_accepts_dict(self):
        msg = {"task_id": "T-1", "result": "42"}
        self.assertTrue(rt.verify(self.pub, msg, rt.sign(self.priv, msg)))

    def test_verify_fails_on_modified_data(self):
        msg = {"task_id": "T-1", "result": "42"}
        sig = rt.sign(self.priv, msg)
        self.assertFalse(rt.verify(self.pub, dict(msg, result="43"), sig))

    def test_verify_fails_with_wrong_public_key(self):
        data = b"authentic message"
        sig = rt.sign(self.priv, data)
        self.assertFalse(rt.verify(self.attacker_pub, data, sig))

    def test_verify_fails_on_forged_signature(self):
        data = b"authentic message"
        forged = rt.sign(self.attacker_priv, data)
        self.assertFalse(rt.verify(self.pub, data, forged))

    def test_verify_handles_garbage_signature(self):
        """验签对垃圾输入返回 False，而不是抛异常炸掉调用方。"""
        self.assertFalse(rt.verify(self.pub, b"data", b""))
        self.assertFalse(rt.verify(self.pub, b"data", b"\x00" * 256))

    def test_pss_signature_is_randomized(self):
        """PSS 带随机盐，同一数据两次签名不同，但都能验过。"""
        data = b"same data"
        s1, s2 = rt.sign(self.priv, data), rt.sign(self.priv, data)
        self.assertNotEqual(s1, s2)
        self.assertTrue(rt.verify(self.pub, data, s1))
        self.assertTrue(rt.verify(self.pub, data, s2))


class TestEncryptThenSignChain(unittest.TestCase):
    """模拟真实链路：发送方签名 + 加密，接收方解密 + 验签 + 校验摘要。"""

    @classmethod
    def setUpClass(cls):
        cls.sender_priv, cls.sender_pub = rt.generate_keypair(2048)
        cls.recv_priv, cls.recv_pub = rt.generate_keypair(2048)

    def test_full_secure_channel(self):
        message = {"task_id": "T-9", "payload": "把这段文字翻译成英文", "sender": "controller"}
        digest = ht.message_digest(message)
        signature = rt.sign(self.sender_priv, message)
        envelope = rt.encrypt(self.recv_pub, message)

        # --- 接收方 ---
        import json

        received = json.loads(rt.decrypt(self.recv_priv, envelope))
        self.assertTrue(rt.verify(self.sender_pub, received, signature))
        self.assertTrue(ht.verify_digest(received, digest))
        self.assertEqual(received, message)

    def test_mitm_replaces_payload_is_caught(self):
        """中间人换掉正文后重新加密：解密能成功，但验签与摘要校验必定失败。"""
        message = {"task_id": "T-9", "payload": "原始指令", "sender": "controller"}
        digest = ht.message_digest(message)
        signature = rt.sign(self.sender_priv, message)

        forged = dict(message, payload="被篡改的指令")
        envelope = rt.encrypt(self.recv_pub, forged)

        import json

        received = json.loads(rt.decrypt(self.recv_priv, envelope))
        self.assertFalse(rt.verify(self.sender_pub, received, signature))
        self.assertFalse(ht.verify_digest(received, digest))


if __name__ == "__main__":
    unittest.main(verbosity=2)
