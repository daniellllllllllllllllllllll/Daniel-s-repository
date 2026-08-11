"""blockchain 模块测试。

分两类：

* **离线部分**（总是执行）：ABI 完整性、摘要转换、NullLedger、
  web3 7 API 契约、错误提示质量。
* **在线部分**（需要 Ganache/anvil）：真实存证 → 读回 → 校验。
  RPC 不可达时自动 skip，不会让没装 Node.js 的环境测试变红。
"""

from __future__ import annotations

import json
import unittest

from blockchain import chain_client as cc
from config import settings
from crypto import hash_tool as ht

# ---------------------------------------------------------------------------
# 【待 Ganache 私有链部署启动后，再进行联调、合约部署测试】
#
# 在线测试的启用条件：.env 中 CHAIN_ENABLED=true 且 RPC 实际可达。
# CHAIN_ENABLED=false 时**完全不发起任何网络探测**——避免测试过程中
# 出现非预期的链请求，也避免在没有节点的机器上白等超时。
# ---------------------------------------------------------------------------
_CHAIN_UP = bool(settings.CHAIN_ENABLED) and cc.ChainClient.probe(timeout=2)
_SKIP_REASON = (
    f"私链未就绪。当前 CHAIN_ENABLED={settings.CHAIN_ENABLED}，"
    f"RPC={settings.CHAIN_RPC_URL}。\n"
    "启用步骤：1) ganache --deterministic --port 8545（需先装 Node.js）"
    " 2) python deploy_contract.py 3) .env 里设 CHAIN_ENABLED=true 与 CONTRACT_ADDRESS"
)


class TestAbiIntegrity(unittest.TestCase):
    """手写 ABI 必须与 contract.sol 保持一致——不一致会在运行期才炸。"""

    @classmethod
    def setUpClass(cls):
        cls.abi = json.loads(cc.DEFAULT_ABI_PATH.read_text(encoding="utf-8"))
        cls.by_name = {e.get("name"): e for e in cls.abi if e.get("type") == "function"}

    def test_abi_file_is_valid_json_list(self):
        self.assertIsInstance(self.abi, list)
        self.assertGreater(len(self.abi), 0)

    def test_all_contract_functions_present(self):
        expected = {
            "storeRecord",
            "storeBatch",
            "verifyDigest",
            "getRecord",
            "getIndexByDigest",
            "getTaskRecords",
            "recordCount",
        }
        self.assertEqual(expected, set(self.by_name))

    def test_store_record_signature(self):
        fn = self.by_name["storeRecord"]
        self.assertEqual(
            [i["type"] for i in fn["inputs"]], ["bytes32", "string", "string", "string"]
        )
        self.assertEqual(fn["stateMutability"], "nonpayable")

    def test_verify_digest_is_view_returning_bool(self):
        fn = self.by_name["verifyDigest"]
        self.assertEqual(fn["stateMutability"], "view")
        self.assertEqual(fn["outputs"][0]["type"], "bool")

    def test_get_record_returns_six_fields(self):
        """与 contract.sol 的 getRecord 返回值逐一对应。"""
        fn = self.by_name["getRecord"]
        self.assertEqual(
            [o["type"] for o in fn["outputs"]],
            ["bytes32", "string", "string", "string", "uint256", "address"],
        )

    def test_record_stored_event_present(self):
        events = [e for e in self.abi if e.get("type") == "event"]
        self.assertEqual([e["name"] for e in events], ["RecordStored"])

    def test_abi_loads_into_web3_contract_factory(self):
        """ABI 结构合法性的最终判据：web3 能拿它建出合约对象。"""
        from web3 import Web3

        w3 = Web3()  # 无 provider 也能建工厂，纯本地解析
        contract = w3.eth.contract(abi=self.abi)
        self.assertTrue(hasattr(contract.functions, "storeRecord"))
        self.assertTrue(hasattr(contract.functions, "verifyDigest"))


class TestWeb3SevenApiContract(unittest.TestCase):
    """守护 web3 7 的破坏性变更：任何一条失败都说明依赖被换回 6.x。

    详见 blockchain/WEB3_7_MIGRATION.md。
    """

    def test_signed_transaction_uses_snake_case(self):
        """web3 6 是 rawTransaction，web3 7 是 raw_transaction。"""
        from eth_account.datastructures import SignedTransaction

        self.assertIn("raw_transaction", SignedTransaction._fields)
        self.assertNotIn("rawTransaction", SignedTransaction._fields)

    def test_poa_middleware_new_name(self):
        import web3.middleware as mw

        self.assertTrue(hasattr(mw, "ExtraDataToPOAMiddleware"))
        self.assertFalse(hasattr(mw, "geth_poa_middleware"))

    def test_camel_case_helpers_removed(self):
        from web3 import Web3

        self.assertTrue(hasattr(Web3, "to_checksum_address"))
        self.assertFalse(hasattr(Web3, "toChecksumAddress"))
        self.assertTrue(hasattr(Web3, "to_wei"))
        self.assertFalse(hasattr(Web3, "toWei"))

    def test_build_transaction_snake_case(self):
        from web3.contract.contract import ContractFunction

        self.assertTrue(hasattr(ContractFunction, "build_transaction"))
        self.assertFalse(hasattr(ContractFunction, "buildTransaction"))

    def test_web3_rpc_error_exists(self):
        """web3 7 新增的结构化 RPC 异常，6.x 只有裸 ValueError。"""
        import web3.exceptions as ex

        self.assertTrue(hasattr(ex, "Web3RPCError"))


class TestDigestConversion(unittest.TestCase):
    """链下摘要 → 合约 bytes32 的转换。"""

    def test_hex_string_to_bytes32(self):
        digest = ht.message_digest({"a": 1})
        raw = cc.ChainClient._to_bytes32(digest)
        self.assertEqual(len(raw), 32)
        self.assertEqual(raw.hex(), digest)

    def test_accepts_0x_prefix(self):
        digest = ht.message_digest({"a": 1})
        self.assertEqual(
            cc.ChainClient._to_bytes32("0x" + digest), cc.ChainClient._to_bytes32(digest)
        )

    def test_accepts_raw_bytes(self):
        raw = ht.digest_bytes32({"a": 1})
        self.assertEqual(cc.ChainClient._to_bytes32(raw), raw)

    def test_rejects_wrong_length(self):
        with self.assertRaises(ValueError) as ctx:
            cc.ChainClient._to_bytes32("abcd")
        self.assertIn("32 字节", str(ctx.exception))

    def test_rejects_non_hex(self):
        with self.assertRaises(ValueError):
            cc.ChainClient._to_bytes32("zz" * 32)


class TestNullLedger(unittest.TestCase):
    """CHAIN_ENABLED=false 时的空实现。"""

    def setUp(self):
        self.ledger = cc.NullLedger()

    def test_satisfies_ledger_backend_protocol(self):
        self.assertIsInstance(self.ledger, cc.LedgerBackend)

    def test_enabled_is_false(self):
        self.assertFalse(self.ledger.enabled)

    def test_store_reports_skipped_not_success(self):
        """关键：不能把"没上链"伪装成"已上链"。"""
        result = self.ledger.store("ab" * 32, "T-1", "a", "b")
        self.assertEqual(result["status"], "skipped")
        self.assertIsNone(result["tx_hash"])

    def test_verify_always_false(self):
        digest = ht.message_digest({"x": 1})
        self.ledger.store(digest, "T-1", "a", "b")
        self.assertFalse(self.ledger.verify(digest))

    def test_count_zero_and_ping_false(self):
        self.assertEqual(self.ledger.count(), 0)
        self.assertFalse(self.ledger.ping())

    def test_stats_track_skipped_calls(self):
        for i in range(3):
            self.ledger.store(ht.message_digest({"i": i}), "T", "a", "b")
        self.assertEqual(self.ledger.stats["skipped_calls"], 3)
        self.assertEqual(self.ledger.stats["tx_count"], 0)


class TestLedgerFactory(unittest.TestCase):
    def test_disabled_returns_null_ledger(self):
        self.assertIsInstance(cc.get_ledger(enabled=False), cc.NullLedger)

    def test_chain_client_satisfies_protocol(self):
        """ChainClient 未连接也应满足 LedgerBackend 协议（结构化检查）。"""
        for name in ("store", "verify", "count", "ping"):
            self.assertTrue(callable(getattr(cc.ChainClient, name)))

    @unittest.skipIf(_CHAIN_UP, "私链可用时该分支不适用")
    def test_enabled_without_chain_raises_with_guidance(self):
        """链不可达时必须抛出带排查步骤的异常，而不是静默降级。"""
        with self.assertRaises(cc.ChainError) as ctx:
            cc.get_ledger(enabled=True, rpc_url="http://127.0.0.1:59999",
                          private_key="0x" + "11" * 32, contract_address="0x" + "22" * 20)
        message = str(ctx.exception)
        self.assertTrue(
            "ganache" in message.lower() or "CONTRACT_ADDRESS" in message,
            f"异常信息缺少排查指引：{message}",
        )


class TestErrorMessages(unittest.TestCase):
    """错误提示必须能指导用户自行解决，而不是只丢一个堆栈。"""

    def test_unreachable_rpc_message_mentions_ganache(self):
        client = cc.ChainClient(
            rpc_url="http://127.0.0.1:59998",
            private_key="0x" + "11" * 32,
            contract_address="0x" + "22" * 20,
            auto_connect=False,
        )
        with self.assertRaises(cc.ChainUnavailable) as ctx:
            client.connect()
        msg = str(ctx.exception)
        self.assertIn("ganache", msg.lower())
        self.assertIn("CHAIN_ENABLED", msg)

    def test_missing_contract_address_message(self):
        client = cc.ChainClient(
            rpc_url=settings.CHAIN_RPC_URL,
            private_key="0x" + "11" * 32,
            contract_address="",
            auto_connect=False,
        )
        # 未连接时直接调用绑定逻辑，验证提示内容
        with self.assertRaises(cc.ContractNotDeployed) as ctx:
            client._bind_contract()
        self.assertIn("deploy_contract.py", str(ctx.exception))

    def test_missing_abi_file(self):
        with self.assertRaises(cc.ContractNotDeployed):
            cc.ChainClient(abi_path="blockchain/does_not_exist.json", auto_connect=False)

    def test_ping_on_unconnected_client_is_false_not_raise(self):
        client = cc.ChainClient(auto_connect=False)
        self.assertFalse(client.ping())


@unittest.skipUnless(_CHAIN_UP, _SKIP_REASON)
class TestOnChainStorage(unittest.TestCase):
    """真实链上读写。需要 Ganache/anvil + 已部署合约。"""

    @classmethod
    def setUpClass(cls):
        if not settings.CONTRACT_ADDRESS:
            raise unittest.SkipTest("未配置 CONTRACT_ADDRESS，请先运行 python deploy_contract.py")
        cls.client = cc.ChainClient()

    def test_ping(self):
        self.assertTrue(self.client.ping())

    def test_store_then_verify(self):
        """核心链路：链下算摘要 → 上链 → 链上校验通过。"""
        message = {"task_id": "T-UT", "content": "单元测试消息", "nonce": id(self)}
        digest = ht.message_digest(message)

        self.assertFalse(self.client.verify(digest))  # 上链前不存在
        result = self.client.store(digest, "T-UT", "controller", "agent-1")

        self.assertEqual(result["status"], "success")
        self.assertIsNotNone(result["tx_hash"])
        self.assertGreater(result["gas_used"], 0)
        self.assertTrue(self.client.verify(digest))  # 上链后校验通过

    def test_tampered_message_fails_verification(self):
        """防篡改的链上证据：改过的消息重算摘要，链上查不到。"""
        message = {"task_id": "T-UT2", "content": "原始内容", "nonce": id(self)}
        self.client.store(ht.message_digest(message), "T-UT2", "controller", "agent-1")

        tampered = dict(message, content="被篡改的内容")
        self.assertFalse(self.client.verify(ht.message_digest(tampered)))

    def test_read_back_record(self):
        message = {"task_id": "T-UT3", "content": "读回测试", "nonce": id(self)}
        digest = ht.message_digest(message)
        self.client.store(digest, "T-UT3", "controller", "agent-2")

        index = self.client.count() - 1
        record = self.client.get_record(index)
        self.assertEqual(record["digest"], digest)
        self.assertEqual(record["task_id"], "T-UT3")
        self.assertEqual(record["sender"], "controller")
        self.assertEqual(record["receiver"], "agent-2")

    def test_count_increases(self):
        before = self.client.count()
        self.client.store(
            ht.message_digest({"n": before, "u": id(self)}), "T-UT4", "a", "b"
        )
        self.assertEqual(self.client.count(), before + 1)

    def test_duplicate_digest_rejected(self):
        """合约拒绝重复摘要——天然防重放。"""
        digest = ht.message_digest({"dup": id(self)})
        self.client.store(digest, "T-DUP", "a", "b")
        with self.assertRaises(cc.ChainTxError):
            self.client.store(digest, "T-DUP", "a", "b")

    def test_batch_store(self):
        digests = [ht.message_digest({"batch": i, "u": id(self)}) for i in range(3)]
        result = self.client.store_batch(digests, "T-BATCH", "controller", "agent-1")
        self.assertEqual(result["count"], 3)
        for d in digests:
            self.assertTrue(self.client.verify(d))

    def test_task_records_lookup(self):
        task_id = f"T-TRACE-{id(self)}"
        for i in range(2):
            self.client.store(ht.message_digest({"t": task_id, "i": i}), task_id, "a", "b")
        self.assertEqual(len(self.client.get_task_records(task_id)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
