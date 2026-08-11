"""区块链交互客户端 —— 适配 **web3.py 7.16.0**。

.. warning::
   **【待 Ganache 私有链部署启动后，再进行联调、合约部署测试】**

   本模块代码已按 web3 7.16.0 的 API 写完，但本机尚未创建启动 Ganache 私有链，
   没有可用 RPC 节点，因此**链上部分未经真机联调**。当前状态下：

   * ``.env`` 中 ``CHAIN_ENABLED=false`` → :func:`get_ledger` 返回 :class:`NullLedger`，
     整个项目其余模块（加密、通信、LLM、智能体、对照实验）完全不受影响，可正常运行；
   * ``tests/test_chain_store.py`` 的在线用例自动 skip，离线用例（ABI 完整性、
     摘要转换、web3 7 API 契约、错误提示）照常执行；
   * 所有连接失败都会抛出带排查步骤的 :class:`ChainUnavailable`，**绝不静默降级**。

   待私链就绪后的联调步骤见 README「区块链联调」一节。

与 web3.py 6.x 的 API 差异详见同目录的 ``WEB3_7_MIGRATION.md``。
本文件所有涉及差异的位置都用 ``# [web3-7]`` 标注，方便回溯。

架构
----
``LedgerBackend`` 是账本抽象接口，:class:`ChainClient`（以太坊/Ganache）与
:class:`NullLedger`（关闭上链）都实现它。阶段4 接入 FISCO BCOS 时，
只需再加一个实现类，上层 Controller 一行都不用改。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from web3 import Web3
from web3.exceptions import ContractLogicError, TimeExhausted, Web3RPCError

# [web3-7] 中间件更名：6.x 是 `geth_poa_middleware`（函数），
#          7.x 是 `ExtraDataToPOAMiddleware`（类）。旧名在 7.x 已完全移除。
from web3.middleware import ExtraDataToPOAMiddleware

from config import BASE_DIR, get_logger, settings
from crypto import hash_tool as ht

logger = get_logger("blkmac.chain")

__all__ = [
    "ChainError",
    "ChainUnavailable",
    "ContractNotDeployed",
    "ChainTxError",
    "LedgerBackend",
    "ChainClient",
    "NullLedger",
    "get_ledger",
    "DEFAULT_ABI_PATH",
]

DEFAULT_ABI_PATH = BASE_DIR / "blockchain" / "contract_abi.json"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ChainError(Exception):
    """链交互错误基类。"""


class ChainUnavailable(ChainError):
    """RPC 节点连不上。绝不静默降级——静默失败会让"已上链"变成谎言。"""


class ContractNotDeployed(ChainError):
    """未配置合约地址，或该地址上没有合约代码。"""


class ChainTxError(ChainError):
    """交易被拒绝、revert 或等待回执超时。"""


# ---------------------------------------------------------------------------
# 账本抽象接口（阶段4 换 FISCO BCOS 的接入点）
# ---------------------------------------------------------------------------


@runtime_checkable
class LedgerBackend(Protocol):
    """账本后端协议。任何实现了这四个方法的对象都可以作为账本使用。"""

    enabled: bool

    def store(self, digest: str, task_id: str, sender: str, receiver: str) -> dict: ...

    def verify(self, digest: str) -> bool: ...

    def count(self) -> int: ...

    def ping(self) -> bool: ...


# ---------------------------------------------------------------------------
# 以太坊 / Ganache 实现
# ---------------------------------------------------------------------------


class ChainClient:
    """Web3 客户端，封装 BlkMacLedger 合约的读写。

    典型用法::

        client = ChainClient()          # 从 .env 读配置
        receipt = client.store(digest, "T-1", "controller", "agent-2")
        assert client.verify(digest)
    """

    def __init__(
        self,
        rpc_url: str | None = None,
        private_key: str | None = None,
        contract_address: str | None = None,
        abi_path: str | Path | None = None,
        chain_id: int | None = None,
        gas_limit: int | None = None,
        tx_timeout: int | None = None,
        poa: bool = True,
        connect_timeout: int = 10,
        auto_connect: bool = True,
    ) -> None:
        self.rpc_url = rpc_url or settings.CHAIN_RPC_URL
        self.private_key = private_key if private_key is not None else settings.CHAIN_PRIVATE_KEY
        self.contract_address_raw = (
            contract_address if contract_address is not None else settings.CONTRACT_ADDRESS
        )
        self.abi_path = Path(abi_path) if abi_path else DEFAULT_ABI_PATH
        self.chain_id = chain_id if chain_id is not None else settings.CHAIN_ID
        self.gas_limit = gas_limit or settings.CHAIN_GAS_LIMIT
        self.tx_timeout = tx_timeout or settings.CHAIN_TX_TIMEOUT
        self.poa = poa
        self.connect_timeout = connect_timeout
        self.enabled = True

        self.w3: Web3 | None = None
        self.account: Any = None
        self.contract: Any = None
        self._nonce_lock = threading.Lock()
        self._tx_count = 0
        self._total_gas = 0

        self.abi = self._load_abi()

        if auto_connect:
            self.connect()

    # -- 初始化 -----------------------------------------------------------

    def _load_abi(self) -> list:
        if not self.abi_path.exists():
            raise ContractNotDeployed(f"ABI 文件不存在：{self.abi_path}")
        try:
            return json.loads(self.abi_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ContractNotDeployed(f"ABI 文件不是合法 JSON：{exc}") from exc

    def connect(self) -> "ChainClient":
        """建立 RPC 连接并绑定合约。失败时抛出带排查指引的异常。"""
        # [web3-7] HTTPProvider 的 request_kwargs 用法不变
        provider = Web3.HTTPProvider(
            self.rpc_url, request_kwargs={"timeout": self.connect_timeout}
        )
        w3 = Web3(provider)

        # [web3-7] 注入 POA 中间件。Ganache / Geth --dev / FISCO 等私链的
        #          extraData 字段超过 32 字节，不注入会在 get_block 时抛
        #          ExtraDataLengthError。
        if self.poa:
            try:
                w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            except Exception as exc:  # 重复注入等情况不致命
                logger.debug("POA 中间件注入跳过：%s", exc)

        if not w3.is_connected():
            raise ChainUnavailable(
                f"无法连接以太坊节点 {self.rpc_url}\n"
                f"排查步骤：\n"
                f"  1. 确认已启动私链： ganache --deterministic --port 8545\n"
                f"     （ganache 是 npm 包，需要先安装 Node.js；也可用 anvil 替代）\n"
                f"  2. 确认 .env 里 CHAIN_RPC_URL 指向正确端口\n"
                f"  3. 若暂时不需要上链，把 .env 的 CHAIN_ENABLED 设为 false"
            )

        self.w3 = w3

        if not self.private_key:
            raise ChainUnavailable(
                "未配置 CHAIN_PRIVATE_KEY。请从 Ganache 启动日志里复制第一个账户的私钥填入 .env"
            )
        try:
            self.account = w3.eth.account.from_key(self.private_key)
        except Exception as exc:
            raise ChainUnavailable(f"CHAIN_PRIVATE_KEY 格式非法：{exc}") from exc

        self._bind_contract()
        logger.info(
            "已连接 %s（chain_id=%s），账户 %s，合约 %s",
            self.rpc_url,
            self.chain_id,
            self.account.address,
            self.contract.address,
        )
        return self

    def _bind_contract(self) -> None:
        if not self.contract_address_raw:
            raise ContractNotDeployed(
                "未配置 CONTRACT_ADDRESS。请先执行： python deploy_contract.py\n"
                "然后把输出的合约地址填入 .env 的 CONTRACT_ADDRESS"
            )
        # [web3-7] toChecksumAddress -> to_checksum_address（6.x 的驼峰别名已移除）
        address = Web3.to_checksum_address(self.contract_address_raw)

        code = self.w3.eth.get_code(address)
        if not code or code == b"" or code == b"\x00":
            raise ContractNotDeployed(
                f"地址 {address} 上没有合约代码。\n"
                f"常见原因：Ganache 重启后链上状态被清空，需要重新部署合约。"
            )
        self.contract = self.w3.eth.contract(address=address, abi=self.abi)

    # -- 健康检查 ---------------------------------------------------------

    def ping(self) -> bool:
        """探活，不抛异常。测试用它决定是否 skip。"""
        try:
            return bool(self.w3 and self.w3.is_connected())
        except Exception:
            return False

    @staticmethod
    def probe(rpc_url: str | None = None, timeout: int = 3) -> bool:
        """不构造完整客户端，仅探测 RPC 是否可达。"""
        try:
            w3 = Web3(
                Web3.HTTPProvider(rpc_url or settings.CHAIN_RPC_URL,
                                  request_kwargs={"timeout": timeout})
            )
            return w3.is_connected()
        except Exception:
            return False

    # -- 写入 -------------------------------------------------------------

    @staticmethod
    def _to_bytes32(digest: str | bytes) -> bytes:
        """把十六进制摘要转成合约需要的 32 字节。"""
        if isinstance(digest, (bytes, bytearray, memoryview)):
            raw = bytes(digest)
        else:
            raw = bytes.fromhex(str(digest).removeprefix("0x"))
        if len(raw) != ht.DIGEST_SIZE:
            raise ValueError(f"摘要必须是 {ht.DIGEST_SIZE} 字节，实际 {len(raw)}")
        return raw

    def _send(self, fn, description: str) -> dict:
        """构造 → 签名 → 广播 → 等回执。返回结构化结果。"""
        if self.w3 is None or self.contract is None:
            raise ChainUnavailable("客户端尚未连接，请先调用 connect()")

        start = time.perf_counter()
        with self._nonce_lock:  # 并发提交时 nonce 必须串行分配，否则交易互相顶掉
            try:
                # [web3-7] "pending" 保证连续提交拿到递增的 nonce
                nonce = self.w3.eth.get_transaction_count(self.account.address, "pending")
                # [web3-7] buildTransaction -> build_transaction
                tx = fn.build_transaction(
                    {
                        "from": self.account.address,
                        "nonce": nonce,
                        "gas": self.gas_limit,
                        "gasPrice": self.w3.eth.gas_price,
                        "chainId": self.chain_id,
                    }
                )
                signed = self.w3.eth.account.sign_transaction(tx, self.private_key)
                # [web3-7] signed.rawTransaction -> signed.raw_transaction
                #          这是升级时最容易踩的坑：6.x 的驼峰属性在 7.x 直接 AttributeError
                tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            except ContractLogicError as exc:
                raise ChainTxError(f"{description} 被合约拒绝（revert）：{exc}") from exc
            except Web3RPCError as exc:
                # [web3-7] 新增异常类型。6.x 的 RPC 错误统一是裸 ValueError，
                #          只能靠解析错误字符串区分，7.x 有了专用类型。
                raise ChainTxError(f"{description} 被节点拒绝：{exc}") from exc

        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=self.tx_timeout)
        except TimeExhausted as exc:
            raise ChainTxError(
                f"{description} 等待回执超时（{self.tx_timeout}s），交易哈希 {tx_hash.hex()}"
            ) from exc

        if receipt["status"] != 1:
            raise ChainTxError(f"{description} 执行失败，交易哈希 {tx_hash.hex()}")

        elapsed_ms = (time.perf_counter() - start) * 1000
        self._tx_count += 1
        self._total_gas += receipt["gasUsed"]

        return {
            "tx_hash": tx_hash.hex(),
            "block_number": receipt["blockNumber"],
            "gas_used": receipt["gasUsed"],
            "elapsed_ms": round(elapsed_ms, 2),
            "status": "success",
        }

    def store(self, digest: str | bytes, task_id: str, sender: str, receiver: str) -> dict:
        """把一条交互记录的摘要上链存证。"""
        fn = self.contract.functions.storeRecord(
            self._to_bytes32(digest), str(task_id), str(sender), str(receiver)
        )
        result = self._send(fn, f"存证 {ht.short(digest)}")
        result["digest"] = (
            digest.hex() if isinstance(digest, (bytes, bytearray)) else str(digest)
        )
        logger.debug("存证成功 %s -> block %s", ht.short(digest), result["block_number"])
        return result

    def store_batch(
        self, digests: list[str | bytes], task_id: str, sender: str, receiver: str
    ) -> dict:
        """一笔交易批量存证，用于把整轮任务的记录一次写完，显著降低总延迟。"""
        if not digests:
            raise ValueError("批量存证的摘要列表不能为空")
        fn = self.contract.functions.storeBatch(
            [self._to_bytes32(d) for d in digests], str(task_id), str(sender), str(receiver)
        )
        result = self._send(fn, f"批量存证 {len(digests)} 条")
        result["count"] = len(digests)
        return result

    # -- 读取（view 调用，不花 gas）----------------------------------------

    def verify(self, digest: str | bytes) -> bool:
        """校验摘要是否已存在于链上账本。"""
        if self.contract is None:
            raise ChainUnavailable("客户端尚未连接")
        return bool(self.contract.functions.verifyDigest(self._to_bytes32(digest)).call())

    def count(self) -> int:
        if self.contract is None:
            raise ChainUnavailable("客户端尚未连接")
        return int(self.contract.functions.recordCount().call())

    def get_record(self, index: int) -> dict:
        if self.contract is None:
            raise ChainUnavailable("客户端尚未连接")
        try:
            digest, task_id, sender, receiver, timestamp, submitter = (
                self.contract.functions.getRecord(int(index)).call()
            )
        except ContractLogicError as exc:
            raise ChainTxError(f"读取下标 {index} 失败：{exc}") from exc
        return {
            "index": int(index),
            "digest": digest.hex(),
            "task_id": task_id,
            "sender": sender,
            "receiver": receiver,
            "timestamp": timestamp,
            "submitter": submitter,
        }

    def get_task_records(self, task_id: str) -> list[int]:
        if self.contract is None:
            raise ChainUnavailable("客户端尚未连接")
        return [int(i) for i in self.contract.functions.getTaskRecords(str(task_id)).call()]

    # -- 统计 -------------------------------------------------------------

    @property
    def stats(self) -> dict:
        return {
            "tx_count": self._tx_count,
            "total_gas": self._total_gas,
            "avg_gas": self._total_gas / self._tx_count if self._tx_count else 0,
        }

    def __repr__(self) -> str:
        addr = self.contract.address if self.contract is not None else "<未绑定>"
        return f"<ChainClient rpc={self.rpc_url} contract={addr}>"


# ---------------------------------------------------------------------------
# 空实现：CHAIN_ENABLED=false 时使用
# ---------------------------------------------------------------------------


class NullLedger:
    """不做任何存证的账本实现。

    存在的意义是让上层代码不必到处写 ``if chain_enabled:``。
    它**不是**本地链模拟——``verify()`` 恒为 False，
    实验结果里会明确标注 ``chain_enabled=False``，不会把"没上链"伪装成"已上链"。
    """

    enabled = False

    def __init__(self) -> None:
        self._calls = 0

    def store(self, digest: str, task_id: str, sender: str, receiver: str) -> dict:
        self._calls += 1
        return {
            "tx_hash": None,
            "block_number": None,
            "gas_used": 0,
            "elapsed_ms": 0.0,
            "status": "skipped",
            "digest": str(digest),
        }

    def store_batch(self, digests, task_id: str, sender: str, receiver: str) -> dict:
        self._calls += 1
        return {"tx_hash": None, "status": "skipped", "count": len(digests), "gas_used": 0,
                "elapsed_ms": 0.0, "block_number": None}

    def verify(self, digest: str) -> bool:
        return False

    def count(self) -> int:
        return 0

    def ping(self) -> bool:
        return False

    @property
    def stats(self) -> dict:
        return {"tx_count": 0, "total_gas": 0, "avg_gas": 0, "skipped_calls": self._calls}

    def __repr__(self) -> str:
        return "<NullLedger 上链已关闭>"


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def get_ledger(enabled: bool | None = None, **kwargs) -> LedgerBackend:
    """按配置返回账本后端。

    阶段4 接入 FISCO BCOS 时，在这里按 ``settings.LEDGER_BACKEND`` 分支即可。
    """
    enabled = settings.CHAIN_ENABLED if enabled is None else enabled
    if not enabled:
        logger.info("上链已关闭（CHAIN_ENABLED=false），使用 NullLedger")
        return NullLedger()

    backend = settings.LEDGER_BACKEND
    if backend == "fisco":
        raise NotImplementedError(
            "FISCO BCOS 后端属于阶段4 改良项，尚未实现。"
            "接入方式：新建 FiscoLedger 类实现 LedgerBackend 协议，在此处返回。"
        )
    if backend != "eth":
        raise ChainError(f"未知的 LEDGER_BACKEND={backend!r}，可选：eth / fisco")

    return ChainClient(**kwargs)
