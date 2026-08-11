#!/usr/bin/env python3
"""链上合约只读校验（**不发起任何交易**）。

只做三类操作，全部不改变链上状态、不消耗 gas：

1. ``eth_getCode``     —— 读取地址上的合约运行时字节码
2. 函数选择器比对      —— 把 contract_abi.json 里每个函数的 4 字节选择器
                          在运行时字节码中查找，确认手写 ABI 与链上合约一致
3. ``view`` 函数调用   —— recordCount / verifyDigest / getRecord，纯读

**不做**的事：storeRecord、storeBatch 等写入操作，以及任何形式的交易广播。

    python verify_chain_readonly.py
"""

from __future__ import annotations

import json
import sys

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware  # [web3-7] 见 WEB3_7_MIGRATION.md

from blockchain.chain_client import DEFAULT_ABI_PATH
from config import settings
from crypto import hash_tool as ht


def _ok(flag: bool) -> str:
    return "[OK]  " if flag else "[!!]  "


def function_selector(w3: Web3, abi_entry: dict) -> str:
    """按 ABI 计算函数的 4 字节选择器（keccak256(签名) 的前 4 字节）。"""
    types = ",".join(i["type"] for i in abi_entry.get("inputs", []))
    signature = f"{abi_entry['name']}({types})"
    return w3.keccak(text=signature)[:4].hex()


def main() -> int:
    print()
    print("=" * 72)
    print("  BlkMacLedger 合约只读校验（不发起任何交易）")
    print("=" * 72)

    problems: list[str] = []

    # ---- 连接 ----
    w3 = Web3(Web3.HTTPProvider(settings.CHAIN_RPC_URL, request_kwargs={"timeout": 10}))
    try:
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    except Exception:
        pass

    if not w3.is_connected():
        print(f"  [!!]  连不上 {settings.CHAIN_RPC_URL}，私链未启动")
        return 1

    print(f"\n[1] 节点")
    print(f"  {_ok(True)}RPC            {settings.CHAIN_RPC_URL}")
    print(f"  {_ok(True)}chainId        {w3.eth.chain_id}")
    print(f"  {_ok(True)}区块高度       {w3.eth.block_number}")

    if not settings.CONTRACT_ADDRESS:
        print("\n  [!!]  .env 里没有 CONTRACT_ADDRESS，先运行 deploy_contract.py")
        return 1

    address = Web3.to_checksum_address(settings.CONTRACT_ADDRESS)

    # ---- 2. 合约字节码 ----
    print(f"\n[2] 合约字节码（eth_getCode，只读）")
    code = w3.eth.get_code(address)
    has_code = len(code) > 2
    print(f"  {_ok(has_code)}地址           {address}")
    print(f"  {_ok(has_code)}运行时字节码   {len(code)} 字节")
    if not has_code:
        print("        该地址上没有合约。常见原因：Ganache 重启后链上状态被清空，")
        print("        需要重新 python deploy_contract.py 并回填 CONTRACT_ADDRESS。")
        return 1

    # ---- 3. ABI 与链上合约比对 ----
    print(f"\n[3] 手写 ABI 与链上合约比对（函数选择器）")
    abi = json.loads(DEFAULT_ABI_PATH.read_text(encoding="utf-8"))
    functions = [e for e in abi if e.get("type") == "function"]
    code_hex = code.hex()

    for entry in functions:
        selector = function_selector(w3, entry)
        present = selector in code_hex
        types = ",".join(i["type"] for i in entry.get("inputs", []))
        print(f"  {_ok(present)}0x{selector}  {entry['name']}({types})")
        if not present:
            problems.append(
                f"ABI 里的 {entry['name']} 在链上合约中不存在 —— "
                f"contract.sol 改过但没同步 ABI？用 deploy_contract.py --save-abi 重生成"
            )

    # ---- 4. view 函数调用（纯读，不消耗 gas）----
    print(f"\n[4] view 函数调用（不改变状态、不消耗 gas）")
    contract = w3.eth.contract(address=address, abi=abi)

    count = contract.functions.recordCount().call()
    print(f"  {_ok(True)}recordCount()              {count}")

    # 一个必定不存在的摘要，verifyDigest 应返回 False
    absent = ht.digest_bytes32({"绝不会被存证的内容": "readonly-probe"})
    absent_result = contract.functions.verifyDigest(absent).call()
    print(f"  {_ok(absent_result is False)}verifyDigest(不存在的摘要) {absent_result}"
          f"  （期望 False）")
    if absent_result is not False:
        problems.append("verifyDigest 对未存证的摘要返回了 True，合约逻辑异常")

    if count > 0:
        digest, task_id, sender, receiver, timestamp, submitter = (
            contract.functions.getRecord(0).call()
        )
        print(f"\n  第 0 条记录（deploy_contract.py 的冒烟测试写入）：")
        print(f"    digest      {ht.short(digest)}")
        print(f"    taskId      {task_id}")
        print(f"    sender      {sender}")
        print(f"    receiver    {receiver}")
        print(f"    timestamp   {timestamp}")
        print(f"    submitter   {submitter}")

        # 用链上读回的摘要反查，应当返回 True —— 存证与校验闭环
        exists = contract.functions.verifyDigest(digest).call()
        print(f"\n  {_ok(exists)}verifyDigest(链上第0条的摘要)  {exists}  （期望 True）")
        if not exists:
            problems.append("链上已有记录，但 verifyDigest 查不到，索引映射异常")

        index = contract.functions.getIndexByDigest(digest).call()
        print(f"  {_ok(index == 0)}getIndexByDigest(同一摘要)     {index}  （期望 0）")
        if index != 0:
            problems.append("getIndexByDigest 返回的下标与实际不符")

        # 防篡改验证：把记录改一个字节，重算摘要，链上必须查不到
        tampered = ht.digest_bytes32({"deploy": "smoke-test-TAMPERED"})
        tampered_found = contract.functions.verifyDigest(tampered).call()
        print(f"  {_ok(tampered_found is False)}verifyDigest(篡改后的摘要)     "
              f"{tampered_found}  （期望 False = 防篡改生效）")
        if tampered_found is not False:
            problems.append("篡改后的摘要竟然在链上查到了，防篡改失效")
    else:
        print("\n  [注]  链上还没有记录。deploy_contract.py 的冒烟测试会写入第一条；")
        print("        若你用了 --no-smoke-test，这里为 0 属正常。")

    # ---- 结论 ----
    print()
    print("=" * 72)
    if problems:
        print(f"  发现 {len(problems)} 个问题：")
        for i, p in enumerate(problems, 1):
            print(f"     {i}. {p}")
    else:
        print("  只读校验全部通过：")
        print("     - 合约已部署且字节码非空")
        print("     - 手写 ABI 的全部函数选择器都能在链上合约中找到")
        print("     - view 调用正常，存证/校验/防篡改逻辑闭环")
        print()
        print("  下一步（会发起真实交易，需你手动执行）：")
        print("     .\\venv\\Scripts\\python.exe -m unittest tests.test_chain_store -v")
        print("     .\\venv\\Scripts\\python.exe -m controller.main_controller --mode all --chain")
    print("=" * 72)
    print()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
