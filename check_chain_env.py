#!/usr/bin/env python3
"""链上环境就绪检查（只读，不发起任何交易）。

用途：在做区块链联调前，一条命令看清还差什么。
本脚本**不打印任何私钥或密钥明文**，只报告「是否已配置」。

    python check_chain_env.py
"""

from __future__ import annotations

import shutil
import sys

from config import BASE_DIR, settings


def _mark(ok: bool) -> str:
    return "[OK]  " if ok else "[缺]  "


def main() -> int:
    print()
    print("=" * 70)
    print("  BlkMaC 链上环境就绪检查")
    print("=" * 70)

    blockers: list[str] = []

    # 先探 RPC：只要链已经在跑，后面的 CLI 检测就只是参考信息。
    # 否则会出现「链明明跑着，却因为当前 shell 的 PATH 陈旧而报未安装」的误判。
    rpc_reachable = False
    try:
        from blockchain.chain_client import ChainClient

        rpc_reachable = ChainClient.probe(timeout=3)
    except Exception:  # noqa: BLE001
        rpc_reachable = False

    # --- 1. 外部工具 ---
    print("\n[1] 私链工具（三选一即可）")
    ganache = shutil.which("ganache") or shutil.which("ganache-cli")
    anvil = shutil.which("anvil")
    node = shutil.which("node")

    print(f"  {_mark(bool(node))}Node.js        {node or '当前 PATH 中未找到'}")
    print(f"  {_mark(bool(ganache))}Ganache        {ganache or '当前 PATH 中未找到'}")
    print(f"  {_mark(bool(anvil))}anvil(Foundry) {anvil or '当前 PATH 中未找到'}")

    if rpc_reachable:
        if not (ganache or anvil):
            print("  [注]  当前 shell 的 PATH 未包含这些程序，但 RPC 已可达，")
            print("        说明私链正在另一个窗口运行 —— 不影响后续操作。")
            print("        若要在本窗口调用，先刷新 PATH：")
            print("        $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine')"
                  " + ';' + [Environment]::GetEnvironmentVariable('Path','User')")
    elif not (ganache or anvil):
        blockers.append("没有可用的私链程序：需装 Ganache（依赖 Node.js）或 Foundry 的 anvil")

    # --- 2. Python 依赖 ---
    print("\n[2] Python 依赖")
    try:
        import web3

        print(f"  {_mark(True)}web3           {web3.__version__}")
    except ImportError:
        print(f"  {_mark(False)}web3           未安装")
        blockers.append("web3 未安装：pip install -r requirements.txt")

    try:
        import solcx  # noqa: F401

        print(f"  {_mark(True)}py-solc-x      已安装")
    except ImportError:
        print(f"  {_mark(False)}py-solc-x      未安装（只有部署合约需要： pip install py-solc-x）")
        blockers.append("py-solc-x 未安装，无法编译 contract.sol")

    # --- 3. 项目文件 ---
    print("\n[3] 合约文件")
    for rel in ("blockchain/contract.sol", "blockchain/contract_abi.json", "deploy_contract.py"):
        exists = (BASE_DIR / rel).exists()
        print(f"  {_mark(exists)}{rel}")
        if not exists:
            blockers.append(f"缺少文件 {rel}")

    # --- 4. .env 配置（只报告是否已填，不打印内容）---
    print("\n[4] .env 配置（不显示明文）")
    has_pk = bool(settings.CHAIN_PRIVATE_KEY)
    has_addr = bool(settings.CONTRACT_ADDRESS)
    print(f"  {_mark(True)}CHAIN_RPC_URL      {settings.CHAIN_RPC_URL}")
    print(f"  {_mark(True)}CHAIN_ID           {settings.CHAIN_ID}")
    print(f"  {_mark(has_pk)}CHAIN_PRIVATE_KEY  {'已配置' if has_pk else '未填写'}")
    print(f"  {_mark(has_addr)}CONTRACT_ADDRESS   {'已配置' if has_addr else '未填写（需先部署）'}")
    print(f"  {_mark(settings.CHAIN_ENABLED)}CHAIN_ENABLED      {settings.CHAIN_ENABLED}")
    if not has_pk:
        blockers.append("CHAIN_PRIVATE_KEY 未填写：从私链启动日志复制第一个账户私钥")
    if not has_addr:
        blockers.append("CONTRACT_ADDRESS 未填写：运行 python deploy_contract.py 后回填")

    # --- 5. RPC 连通性（只读探测，不发交易）---
    print("\n[5] RPC 连通性")
    print(
        f"  {_mark(rpc_reachable)}{settings.CHAIN_RPC_URL} "
        f"{'可达' if rpc_reachable else '不可达（私链未启动）'}"
    )
    if not rpc_reachable:
        blockers.append(f"RPC {settings.CHAIN_RPC_URL} 不可达：私链未启动")
    else:
        # 链已连通时，核对链上实际参数与 .env 是否一致。
        # chainId 对不上会让交易签名被节点拒绝，且报错信息通常很晦涩，
        # 在这里提前发现比在部署时踩坑强得多。
        try:
            from web3 import Web3

            w3 = Web3(
                Web3.HTTPProvider(settings.CHAIN_RPC_URL, request_kwargs={"timeout": 5})
            )
            on_chain_id = w3.eth.chain_id
            accounts = w3.eth.accounts
            block = w3.eth.block_number

            match = on_chain_id == settings.CHAIN_ID
            print(f"  {_mark(match)}链上 chainId       {on_chain_id}"
                  f"{'' if match else f'  ← 与 .env 的 CHAIN_ID={settings.CHAIN_ID} 不一致'}")
            print(f"  {_mark(bool(accounts))}可用账户           {len(accounts)} 个"
                  f"{f'，第一个 {accounts[0]}' if accounts else ''}")
            print(f"  {_mark(True)}当前区块高度       {block}")

            if not match:
                blockers.append(
                    f"chainId 不一致：链上 {on_chain_id}，.env 里 CHAIN_ID={settings.CHAIN_ID}。"
                    f"用 --chain.chainId {settings.CHAIN_ID} 重启私链，或改 .env"
                )
            if not accounts:
                blockers.append("私链没有可用账户，检查启动参数")
        except Exception as exc:  # noqa: BLE001
            print(f"  [错]  读取链上参数失败：{exc}")

    # --- 结论 ---
    print("\n" + "=" * 70)
    if not blockers:
        print("  全部就绪，可以执行：")
        print("     python -m unittest tests.test_chain_store -v")
        print("     python -m controller.main_controller --mode all --chain")
    else:
        print(f"  还差 {len(blockers)} 项：")
        for i, b in enumerate(blockers, 1):
            print(f"     {i}. {b}")
        print()
        print("  详细步骤见 README.md「区块链联调」一节。")
        print("  未就绪不影响其余模块：CHAIN_ENABLED=false 时全流程照常运行。")
    print("=" * 70)
    print()
    return 0 if not blockers else 1


if __name__ == "__main__":
    sys.exit(main())
