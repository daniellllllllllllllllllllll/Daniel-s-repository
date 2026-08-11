"""communication 模块测试：分帧、加密信道、并发、防篡改。"""

from __future__ import annotations

import json
import socket
import struct
import threading
import unittest

from communication import tcp_socket as tc
from crypto import rsa_tool as rt

# 全模块共用两对密钥：2048 位生成较慢，逐用例生成会让测试变得很久
_CTRL_PRIV, _CTRL_PUB = rt.generate_keypair(2048)
_AGENT_PRIV, _AGENT_PUB = rt.generate_keypair(2048)
_EVIL_PRIV, _EVIL_PUB = rt.generate_keypair(2048)


def _echo_handler(message: tc.SecureMessage) -> dict:
    """把收到的 payload 原样回显，附带处理方标识。"""
    return {"echo": message.payload, "handled_by": message.receiver}


class _ChannelFixture(unittest.TestCase):
    """起一个 controller 客户端 + 一个 agent 服务端的现成信道。"""

    handler = staticmethod(_echo_handler)

    def setUp(self):
        self.registry_server = tc.KeyRegistry()
        self.registry_client = tc.KeyRegistry()
        self.registry_server.register("controller", _CTRL_PUB)
        self.registry_client.register("agent-1", _AGENT_PUB)

        self.server = tc.SecureServer(
            node_id="agent-1",
            private_key=_AGENT_PRIV,
            registry=self.registry_server,
            handler=type(self).handler,
            host="127.0.0.1",
            port=0,  # 让系统分配端口，避免测试间端口冲突
        ).start()
        self.addCleanup(self.server.stop)

        self.client = tc.SecureClient("controller", _CTRL_PRIV, self.registry_client, timeout=10)
        self.host, self.port = self.server.address


class TestFraming(unittest.TestCase):
    """长度前缀分帧——TCP 没有消息边界，这层必须自己做。"""

    def setUp(self):
        self.a, self.b = socket.socketpair()
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)

    def test_roundtrip(self):
        tc.send_frame(self.a, b"hello")
        self.assertEqual(tc.recv_frame(self.b), b"hello")

    def test_empty_frame(self):
        tc.send_frame(self.a, b"")
        self.assertEqual(tc.recv_frame(self.b), b"")

    def test_large_frame_split_across_tcp_packets(self):
        """1MB 帧必然被拆成多个 TCP 包，_recv_exact 必须循环读完。"""
        payload = b"x" * (1024 * 1024)
        t = threading.Thread(target=tc.send_frame, args=(self.a, payload), daemon=True)
        t.start()
        self.assertEqual(tc.recv_frame(self.b), payload)
        t.join(timeout=10)

    def test_back_to_back_frames_are_not_glued(self):
        """核心用例：连发三帧不粘包，逐帧读出且顺序正确。"""
        for chunk in (b"one", b"two-two", b"three-three-three"):
            tc.send_frame(self.a, chunk)
        self.assertEqual(tc.recv_frame(self.b), b"one")
        self.assertEqual(tc.recv_frame(self.b), b"two-two")
        self.assertEqual(tc.recv_frame(self.b), b"three-three-three")

    def test_clean_eof_returns_none(self):
        self.a.close()
        self.assertIsNone(tc.recv_frame(self.b))

    def test_truncated_body_raises(self):
        """长度前缀声明 100 字节却只发 5 字节，必须报错而不是静默返回残缺数据。"""
        self.a.sendall(struct.pack(">I", 100) + b"short")
        self.a.close()
        with self.assertRaises(tc.ProtocolError):
            tc.recv_frame(self.b)

    def test_oversized_frame_rejected_without_allocating(self):
        """恶意超长帧头必须被直接拒绝，防止预分配巨大缓冲。"""
        self.a.sendall(struct.pack(">I", 999_999_999))
        with self.assertRaises(tc.ProtocolError) as ctx:
            tc.recv_frame(self.b, max_frame=4096)
        self.assertIn("超过上限", str(ctx.exception))


class TestKeyRegistry(unittest.TestCase):
    def test_register_and_get(self):
        reg = tc.KeyRegistry()
        reg.register("agent-1", _AGENT_PUB)
        self.assertTrue(reg.has("agent-1"))
        self.assertEqual(reg.get("agent-1").export_key(), _AGENT_PUB.export_key())

    def test_register_accepts_pem(self):
        reg = tc.KeyRegistry()
        reg.register("agent-1", rt.export_public_pem(_AGENT_PUB))
        self.assertEqual(reg.get("agent-1").export_key(), _AGENT_PUB.export_key())

    def test_private_key_is_downgraded_to_public(self):
        """误传私钥进来也只保存公钥部分，避免私钥被意外泄露到登记表。"""
        reg = tc.KeyRegistry()
        reg.register("agent-1", _AGENT_PRIV)
        self.assertFalse(reg.get("agent-1").has_private())

    def test_unknown_peer_raises(self):
        reg = tc.KeyRegistry()
        with self.assertRaises(tc.UnknownPeer):
            reg.get("nobody")

    def test_node_ids_sorted(self):
        reg = tc.KeyRegistry()
        reg.register("b", _AGENT_PUB)
        reg.register("a", _CTRL_PUB)
        self.assertEqual(reg.node_ids(), ["a", "b"])
        self.assertEqual(len(reg), 2)


class TestServerLifecycle(unittest.TestCase):
    def test_start_stop_idempotent(self):
        reg = tc.KeyRegistry()
        srv = tc.SecureServer("n1", _AGENT_PRIV, reg, host="127.0.0.1", port=0)
        srv.start()
        first_port = srv.port
        srv.start()  # 重复 start 不应换端口或起第二个线程
        self.assertEqual(srv.port, first_port)
        srv.stop()
        srv.stop()  # 重复 stop 不应抛异常

    def test_context_manager(self):
        reg = tc.KeyRegistry()
        with tc.SecureServer("n1", _AGENT_PRIV, reg, host="127.0.0.1", port=0) as srv:
            self.assertGreater(srv.port, 0)

    def test_port_zero_gets_real_port(self):
        reg = tc.KeyRegistry()
        with tc.SecureServer("n1", _AGENT_PRIV, reg, host="127.0.0.1", port=0) as srv:
            self.assertNotEqual(srv.port, 0)

    def test_bind_conflict_raises_transport_error(self):
        """端口已被占用时必须抛 TransportError，而不是静默劫持端口。

        这条用例守护一个平台差异：Windows 的 SO_REUSEADDR 允许抢占正在监听的
        端口，Unix 不允许。tcp_socket.py 在 Windows 上改用 SO_EXCLUSIVEADDRUSE，
        使两边行为一致——否则两个 Agent 配到同一端口时不会报错，而是消息静默
        跑到错误的节点上，极难排查。
        """
        reg = tc.KeyRegistry()
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        occupied_port = blocker.getsockname()[1]
        self.addCleanup(blocker.close)

        srv = tc.SecureServer("n1", _AGENT_PRIV, reg, host="127.0.0.1", port=occupied_port)
        with self.assertRaises(tc.TransportError) as ctx:
            srv.start()
        self.assertIn("绑定", str(ctx.exception))


class TestSecureChannel(_ChannelFixture):
    """端到端加密链路。"""

    def test_send_receive_roundtrip(self):
        reply = self.client.send(self.host, self.port, "agent-1", {"q": "1+1=?"})
        self.assertEqual(reply.payload["echo"], {"q": "1+1=?"})
        self.assertEqual(reply.sender, "agent-1")

    def test_server_records_decrypted_message(self):
        self.client.send(self.host, self.port, "agent-1", {"task": "T-1"})
        self.assertEqual(len(self.server.received), 1)
        self.assertEqual(self.server.received[0].payload, {"task": "T-1"})
        self.assertEqual(self.server.received[0].sender, "controller")

    def test_chinese_and_long_payload(self):
        """中文 + 超过裸 RSA 上限的长文本，混合加密必须扛得住。"""
        payload = {"desc": "把下面这段话翻译成英文：" + "多智能体协同。" * 500}
        reply = self.client.send(self.host, self.port, "agent-1", payload)
        self.assertEqual(reply.payload["echo"], payload)

    def test_digest_matches_payload(self):
        reply = self.client.send(self.host, self.port, "agent-1", {"a": 1})
        msg = self.server.received[0]
        from crypto import hash_tool as ht

        self.assertEqual(msg.digest, ht.message_digest(msg.payload))
        self.assertEqual(len(reply.digest), 64)

    def test_ledger_record_excludes_plaintext(self):
        """上链记录只带摘要，不能把明文 payload 写上链。"""
        self.client.send(self.host, self.port, "agent-1", {"secret": "不该上链的明文"})
        record = self.server.received[0].ledger_record()
        self.assertNotIn("payload", record)
        self.assertIn("digest", record)
        self.assertNotIn("不该上链的明文", json.dumps(record, ensure_ascii=False))

    def test_multiple_messages_same_connection(self):
        """同一连接连发三条：验证服务端循环收帧不粘包。"""
        payloads = [{"i": i} for i in range(3)]
        replies = self.client.send_many(self.host, self.port, "agent-1", payloads)
        self.assertEqual(len(replies), 3)
        for i, reply in enumerate(replies):
            self.assertEqual(reply.payload["echo"], {"i": i})

    def test_concurrent_clients(self):
        """8 个线程并发请求，服务端每连接一线程必须都能正确处理。"""
        results: list[int] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(i: int):
            try:
                client = tc.SecureClient(
                    "controller", _CTRL_PRIV, self.registry_client, timeout=15
                )
                reply = client.send(self.host, self.port, "agent-1", {"i": i})
                with lock:
                    results.append(reply.payload["echo"]["i"])
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), list(range(8)))

    def test_connect_refused(self):
        free = tc.pick_free_port()
        with self.assertRaises(tc.TransportError):
            self.client.send(self.host, free, "agent-1", {"x": 1})

    def test_unknown_receiver_rejected_before_sending(self):
        with self.assertRaises(tc.UnknownPeer):
            self.client.send(self.host, self.port, "agent-unknown", {"x": 1})


class TestHandshake(unittest.TestCase):
    """明文握手交换公钥，实现自举。"""

    def test_handshake_populates_both_registries(self):
        server_reg = tc.KeyRegistry()
        client_reg = tc.KeyRegistry()

        server = tc.SecureServer(
            "agent-9", _AGENT_PRIV, server_reg, handler=_echo_handler, host="127.0.0.1", port=0
        ).start()
        self.addCleanup(server.stop)

        client = tc.SecureClient("controller", _CTRL_PRIV, client_reg, timeout=10)
        self.assertFalse(client_reg.has("agent-9"))

        peer_id = client.handshake(*server.address)

        self.assertEqual(peer_id, "agent-9")
        self.assertTrue(client_reg.has("agent-9"))  # 客户端拿到了服务端公钥
        self.assertTrue(server_reg.has("controller"))  # 服务端也登记了客户端公钥

        # 握手后即可直接加密通信
        reply = client.send(*server.address, "agent-9", {"ping": True})
        self.assertEqual(reply.payload["echo"], {"ping": True})


class TestTamperDetection(_ChannelFixture):
    """篡改检测——论文"防篡改"要求的直接证据。"""

    def _raw_request(self, envelope: dict) -> dict:
        """绕过 SecureClient，直接把手工构造的信封发给服务端。"""
        with socket.create_connection((self.host, self.port), timeout=10) as sock:
            tc.send_frame(sock, json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
            raw = tc.recv_frame(sock)
        return json.loads(raw.decode("utf-8"))

    def _build_valid_envelope(self, payload) -> dict:
        return tc._build_envelope(
            msg_type=tc.MsgType.TASK,
            sender="controller",
            receiver="agent-1",
            payload=payload,
            private_key=_CTRL_PRIV,
            peer_public_key=_AGENT_PUB,
        )

    def test_baseline_envelope_is_accepted(self):
        """先证明手工构造的合法信封能通过，后面的失败才有意义。"""
        reply = self._raw_request(self._build_valid_envelope({"ok": 1}))
        self.assertNotEqual(reply.get("msg_type"), tc.MsgType.ERROR)

    def test_tampered_ciphertext_rejected(self):
        """改动密文一个字节：AES-GCM 认证标签就会失败。"""
        env = self._build_valid_envelope({"amount": 100})
        raw = bytearray(tc._b64d(env["cipher"]))
        raw[-1] ^= 0x01
        env["cipher"] = tc._b64e(bytes(raw))

        reply = self._raw_request(env)
        self.assertEqual(reply.get("msg_type"), tc.MsgType.ERROR)
        self.assertEqual(len(self.server.received), 0)
        self.assertEqual(len(self.server.tamper_events), 1)

    def test_forged_signature_rejected(self):
        """攻击者用自己的私钥签名，冒充 controller：验签必须失败。"""
        payload = {"cmd": "转账"}
        from crypto import hash_tool as ht

        canonical = ht.canonical_json(payload)
        env = self._build_valid_envelope(payload)
        env["signature"] = tc._b64e(rt.sign(_EVIL_PRIV, canonical))

        reply = self._raw_request(env)
        self.assertEqual(reply.get("msg_type"), tc.MsgType.ERROR)
        self.assertEqual(len(self.server.tamper_events), 1)
        self.assertIn("签名", self.server.tamper_events[0]["reason"])

    def test_swapped_digest_rejected(self):
        """密文与签名都合法，但信封头的 digest 被换掉：摘要校验拦住。"""
        env = self._build_valid_envelope({"v": 1})
        env["digest"] = "0" * 64

        reply = self._raw_request(env)
        self.assertEqual(reply.get("msg_type"), tc.MsgType.ERROR)
        self.assertEqual(len(self.server.tamper_events), 1)
        self.assertIn("摘要", self.server.tamper_events[0]["reason"])

    def test_message_from_unregistered_sender_rejected(self):
        """服务端没登记攻击者公钥，无法验签，直接拒绝。"""
        env = tc._build_envelope(
            msg_type=tc.MsgType.TASK,
            sender="evil",
            receiver="agent-1",
            payload={"x": 1},
            private_key=_EVIL_PRIV,
            peer_public_key=_AGENT_PUB,
        )
        reply = self._raw_request(env)
        self.assertEqual(reply.get("msg_type"), tc.MsgType.ERROR)

    def test_replay_with_wrong_recipient_key_fails(self):
        """用错误的接收方公钥加密（发错人），服务端私钥解不开。"""
        env = tc._build_envelope(
            msg_type=tc.MsgType.TASK,
            sender="controller",
            receiver="agent-1",
            payload={"x": 1},
            private_key=_CTRL_PRIV,
            peer_public_key=_EVIL_PUB,  # 加密给了错误的人
        )
        reply = self._raw_request(env)
        self.assertEqual(reply.get("msg_type"), tc.MsgType.ERROR)

    def test_malformed_envelope_missing_field(self):
        env = self._build_valid_envelope({"x": 1})
        del env["signature"]
        reply = self._raw_request(env)
        self.assertEqual(reply.get("msg_type"), tc.MsgType.ERROR)

    def test_non_json_frame_rejected(self):
        with socket.create_connection((self.host, self.port), timeout=10) as sock:
            tc.send_frame(sock, b"\xff\xfe not json at all")
            raw = tc.recv_frame(sock)
        reply = json.loads(raw.decode("utf-8"))
        self.assertEqual(reply.get("msg_type"), tc.MsgType.ERROR)


class TestHandlerFailure(_ChannelFixture):
    """业务 handler 抛异常时，服务端应回错误帧而不是让连接线程静默死掉。"""

    @staticmethod
    def handler(message):
        raise RuntimeError("业务处理故意失败")

    def test_handler_exception_returns_error_frame(self):
        with self.assertRaises(tc.CommError) as ctx:
            self.client.send(self.host, self.port, "agent-1", {"x": 1})
        self.assertIn("业务处理故意失败", str(ctx.exception))

    def test_server_survives_handler_exception(self):
        """一次失败之后服务端仍然存活，能继续接受新连接。"""
        with self.assertRaises(tc.CommError):
            self.client.send(self.host, self.port, "agent-1", {"x": 1})
        with self.assertRaises(tc.CommError):
            self.client.send(self.host, self.port, "agent-1", {"y": 2})
        self.assertEqual(len(self.server.received), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
