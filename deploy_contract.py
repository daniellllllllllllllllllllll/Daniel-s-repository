#!/usr/bin/env python3
"""把 blockchain/contract.sol 编译并部署到私链（Ganache / anvil）。

.. warning::
   **【待 Ganache 私有链部署启动后，再进行联调、合约部署测试】**

   本脚本会发起真实的链上交易（部署合约 + 冒烟测试存证），
   **必须由使用者手动执行**，任何自动化流程（含单元测试）都不会调用它。
   执行前请确认：私链已启动、``.env`` 中 ``CHAIN_PRIVATE_KEY`` 已填写。

   所有凭证一律从 ``.env`` 读取，脚本不接受命令行传入私钥，
   也不会把私钥打印到终端或日志。

用法::

    # 1. 先启动私链（任选其一）
    ganache --deterministic --port 8545      # 需要 Node.js
    anvil --port 8545                        # 需要 Foundry

    # 2. 把私链输出的第一个账户私钥填入 .env 的 CHAIN_PRIVATE_KEY

    # 3. 部署
    python deploy_contract.py

    # 4. 把打印出来的合约地址填入 .env 的 CONTRACT_ADDRESS

编译器依赖说明
--------------
Solidity 编译需要 ``py-solc-x``，它**不在 requirements.txt 里**——
只有部署这一步才用得到，其余模块（加密、通信、LLM、实验）完全不需要它。
第一次运行时若缺失会给出安装提示；py-solc-x 会自动下载 solc 二进制。

若无法联网下载 solc，可以用 ``--abi-only`` 跳过编译，直接用仓库里
已经手写好的 ``blockchain/contract_abi.json``（此时需要自行提供 bytecode）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware  # [web3-7] 见 WEB3_7_MIGRATION.md

from config import BASE_DIR, get_logger, settings

logger = get_logger("blkmac.deploy")

CONTRACT_PATH = BASE_DIR / "blockchain" / "contract.sol"
ABI_PATH = BASE_DIR / "blockchain" / "contract_abi.json"
BUILD_DIR = BASE_DIR / "blockchain" / "build"
SOLC_VERSION = "0.8.20"


def compile_contract(contract_path: Path = CONTRACT_PATH) -> tuple[list, str]:
    """编译 Solidity 源码，返回 ``(abi, bytecode)``。

    solcx 采用惰性导入：没装也不影响项目其它部分。
    """
    try:
        import solcx  # noqa: PLC0415  —— 惰性导入是有意为之
    except ImportError:
        print(
            "[错误] 缺少 Solidity 编译器绑定 py-solc-x。\n"
            "       它只有部署合约这一步需要，因此没有写进 requirements.txt。\n"
            "       安装： pip install py-solc-x\n",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if not contract_path.exists():
        raise SystemExit(f"[错误] 找不到合约源码：{contract_path}")

    installed = [str(v) for v in solcx.get_installed_solc_versions()]
    if SOLC_VERSION not in installed:
        print(f"[信息] 正在下载 solc {SOLC_VERSION}（首次运行需要联网）...")
        solcx.install_solc(SOLC_VERSION)

    print(f"[信息] 使用 solc {SOLC_VERSION} 编译 {contract_path.name}")
    compiled = solcx.compile_files(
        [str(contract_path)],
        output_values=["abi", "bin"],
        solc_version=SOLC_VERSION,
        optimize=True,
        optimize_runs=200,
    )

    key = next((k for k in compiled if k.endswith(":BlkMacLedger")), None)
    if key is None:
        raise SystemExit(f"[错误] 编译产物里找不到 BlkMacLedger，实际包含：{list(compiled)}")

    return compiled[key]["abi"], compiled[key]["bin"]


def connect() -> tuple[Web3, object]:
    """连接私链并返回 ``(w3, account)``。"""
    w3 = Web3(Web3.HTTPProvider(settings.CHAIN_RPC_URL, request_kwargs={"timeout": 20}))
    try:
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    except Exception:
        pass

    if not w3.is_connected():
        raise SystemExit(
            f"[错误] 连不上 {settings.CHAIN_RPC_URL}\n"
            f"       请先启动私链： ganache --deterministic --port 8545\n"
            f"       （ganache 是 npm 包，需先安装 Node.js；也可用 Foundry 的 anvil）"
        )

    if not settings.CHAIN_PRIVATE_KEY:
        raise SystemExit(
            "[错误] .env 里没有 CHAIN_PRIVATE_KEY。\n"
            "       请从私链启动日志里复制第一个账户的私钥填入。"
        )

    account = w3.eth.account.from_key(settings.CHAIN_PRIVATE_KEY)
    balance = w3.eth.get_balance(account.address)
    print(f"[信息] 已连接 {settings.CHAIN_RPC_URL}")
    print(f"[信息] 部署账户 {account.address}")
    print(f"[信息] 余额 {w3.from_wei(balance, 'ether')} ETH")  # [web3-7] fromWei -> from_wei

    if balance == 0:
        raise SystemExit("[错误] 部署账户余额为 0，无法支付 gas")

    return w3, account


def deploy(w3: Web3, account, abi: list, bytecode: str) -> str:
    """部署合约，返回合约地址。"""
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)

    # [web3-7] buildTransaction -> build_transaction
    tx = contract.constructor().build_transaction(
        {
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 3_000_000,
            "gasPrice": w3.eth.gas_price,
            "chainId": settings.CHAIN_ID,
        }
    )

    signed = w3.eth.account.sign_transaction(tx, settings.CHAIN_PRIVATE_KEY)
    # [web3-7] signed.rawTransaction -> signed.raw_transaction （升级最易踩的坑）
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"[信息] 部署交易已广播：{tx_hash.hex()}")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=settings.CHAIN_TX_TIMEOUT)
    if receipt["status"] != 1:
        raise SystemExit(f"[错误] 部署失败，回执 status={receipt['status']}")

    print(f"[信息] 部署成功，区块 {receipt['blockNumber']}，消耗 gas {receipt['gasUsed']}")
    return receipt["contractAddress"]


def smoke_test(w3: Web3, account, abi: list, address: str) -> None:
    """部署后立刻跑一次存证 + 校验，确认合约真的能用。"""
    from crypto import hash_tool as ht

    contract = w3.eth.contract(address=address, abi=abi)
    digest = ht.digest_bytes32({"deploy": "smoke-test"})

    print("[信息] 冒烟测试：存证一条记录 ...")
    tx = contract.functions.storeRecord(digest, "DEPLOY", "deployer", "ledger").build_transaction(
        {
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": settings.CHAIN_GAS_LIMIT,
            "gasPrice": w3.eth.gas_price,
            "chainId": settings.CHAIN_ID,
        }
    )
    signed = w3.eth.account.sign_transaction(tx, settings.CHAIN_PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=settings.CHAIN_TX_TIMEOUT)

    assert contract.functions.verifyDigest(digest).call(), "存证后校验竟然返回 False"
    assert contract.functions.recordCount().call() >= 1
    print("[信息] 冒烟测试通过：存证 -> 校验 -> 计数 全部正常")


def main() -> int:
    parser = argparse.ArgumentParser(description="部署 BlkMacLedger 合约到私链")
    parser.add_argument(
        "--save-abi",
        action="store_true",
        help="用编译产出的 ABI 覆盖 blockchain/contract_abi.json（改过 .sol 后需要）",
    )
    parser.add_argument("--no-smoke-test", action="store_true", help="跳过部署后的冒烟测试")
    args = parser.parse_args()

    print("=" * 66)
    print("  BlkMacLedger 合约部署")
    print("=" * 66)

    abi, bytecode = compile_contract()

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    (BUILD_DIR / "BlkMacLedger.json").write_text(
        json.dumps({"abi": abi, "bytecode": bytecode}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if args.save_abi:
        ABI_PATH.write_text(json.dumps(abi, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[信息] 已更新 {ABI_PATH.relative_to(BASE_DIR)}")

    w3, account = connect()
    address = deploy(w3, account, abi, bytecode)

    if not args.no_smoke_test:
        smoke_test(w3, account, abi, address)

    print()
    print("=" * 66)
    print(f"  合约地址： {address}")
    print("=" * 66)
    print()
    print("下一步：把上面的地址填入 .env")
    print()
    print(f"    CONTRACT_ADDRESS={address}")
    print("    CHAIN_ENABLED=true")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
