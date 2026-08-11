"""BlkMaC 区块链存证包（适配 web3.py 7.16.0）。

与 web3 6.x 的 API 差异见同目录 WEB3_7_MIGRATION.md。
"""

from blockchain.chain_client import (
    DEFAULT_ABI_PATH,
    ChainClient,
    ChainError,
    ChainTxError,
    ChainUnavailable,
    ContractNotDeployed,
    LedgerBackend,
    NullLedger,
    get_ledger,
)

__all__ = [
    "DEFAULT_ABI_PATH",
    "ChainClient",
    "ChainError",
    "ChainTxError",
    "ChainUnavailable",
    "ContractNotDeployed",
    "LedgerBackend",
    "NullLedger",
    "get_ledger",
]
