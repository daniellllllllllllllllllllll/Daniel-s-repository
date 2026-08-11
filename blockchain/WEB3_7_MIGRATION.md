# web3.py 6.18.0 → 7.16.0 API 差异记录

## 为什么必须升级（不是可选项）

原 `requirements.txt` 钉的是 `web3==6.18.0`。本机 Python 版本是 **3.13.14**，
而 web3 6.x 的传递依赖 **`ckzg`** 和 **`lru-dict`** 都没有 cp313 的预编译 wheel，
pip 会退回源码编译，报：

```
error: Microsoft Visual C++ 14.0 or greater is required
ERROR: Failed building wheel for lru-dict, ckzg
```

本机没有 MSVC 构建工具。实测 web3 **6.x 全系列**（6.0.0 ~ 6.20.4）在 Python 3.13
上都装不上；7.x 全系列依赖均有 cp313 wheel。因此升级到 7.16.0 是唯一可行路径
（另一条路是降到 Python 3.12，但本机 `py --list` 只有 3.13）。

**兼容性风险：** web3 7 是大版本升级，含破坏性变更。下面逐条列出本项目实际用到、
且在两个版本间有差异的 API。代码中所有相关位置都标了 `# [web3-7]` 注释。

---

## 破坏性变更对照表

| # | 用途 | web3 6.18.0 | web3 7.16.0 | 本项目位置 |
|---|------|-------------|-------------|-----------|
| 1 | 已签名交易的原始字节 | `signed.rawTransaction` | **`signed.raw_transaction`** | `chain_client.py::_send`、`deploy_contract.py::deploy` |
| 2 | POA 私链中间件 | `from web3.middleware import geth_poa_middleware`（函数） | **`from web3.middleware import ExtraDataToPOAMiddleware`**（类） | `chain_client.py::connect`、`deploy_contract.py::connect` |
| 3 | 地址校验和 | `Web3.toChecksumAddress()` | **`Web3.to_checksum_address()`** | `chain_client.py::_bind_contract` |
| 4 | 构造合约交易 | `.buildTransaction()` | **`.build_transaction()`** | `chain_client.py::_send`、`deploy_contract.py` |
| 5 | 单位换算 | `Web3.toWei()` / `fromWei()` | **`Web3.to_wei()` / `from_wei()`** | `deploy_contract.py::connect` |
| 6 | RPC 层错误 | 裸 `ValueError`，只能解析字符串区分 | **`web3.exceptions.Web3RPCError`** 专用类型 | `chain_client.py::_send` |
| 7 | 发送裸交易 | `sendRawTransaction`（6.x 已弃用但仍在） | **`send_raw_transaction`**（驼峰别名彻底移除） | `chain_client.py::_send` |

### 已实测确认（非凭记忆）

```
web3 7.16.0 | eth_account 0.13.7
SignedTransaction._fields = ('raw_transaction', 'hash', 'r', 's', 'v')
web3.middleware: has geth_poa_middleware = False
                 has ExtraDataToPOAMiddleware = True
Web3.toChecksumAddress = False   Web3.to_checksum_address = True
Web3.toWei = False               Web3.to_wei = True
ContractFunction.buildTransaction = False   build_transaction = True
web3.exceptions: 新增 Web3RPCError / Web3ValidationError / Web3ValueError / Web3TypeError
```

---

## 逐条说明

### 1. `rawTransaction` → `raw_transaction` ⚠️ 最容易踩

web3 6 的 `SignedTransaction` 是 NamedTuple，字段名为驼峰。web3 7 全面改蛇形，
且**没有保留别名**。旧代码不会报「弃用警告」，而是直接：

```
AttributeError: 'SignedTransaction' object has no attribute 'rawTransaction'
```

网上绝大多数 Ganache + web3 教程仍是 6.x 写法，照抄必然在这一行崩。

```python
# web3 6.x
tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
# web3 7.x
tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
```

### 2. POA 中间件更名且从函数变成类

Ganache、Geth `--dev`、FISCO BCOS 等私链的区块头 `extraData` 字段超过 32 字节，
不注入 POA 中间件时调用 `get_block` 会抛 `ExtraDataLengthError`。

```python
# web3 6.x
from web3.middleware import geth_poa_middleware
w3.middleware_onion.inject(geth_poa_middleware, layer=0)

# web3 7.x
from web3.middleware import ExtraDataToPOAMiddleware
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
```

注意 web3 7 注入的是**类本身**，不是实例，也不是函数。

### 3–5. 驼峰命名全面移除

web3 6 同时提供驼峰与蛇形两套名字（驼峰标记为 deprecated），web3 7 把驼峰**全部删掉**。
凡是 `toXxx` / `fromXxx` / `xxxYyy` 形式的调用都要改。

### 6. 新增结构化 RPC 异常

web3 6 里节点返回的 JSON-RPC 错误统一包成 `ValueError`，想区分「nonce 太低」和
「gas 不足」只能去 `str(exc)` 里做字符串匹配。web3 7 引入 `Web3RPCError`，
本项目在 `_send()` 中据此把节点拒绝与合约 revert（`ContractLogicError`）分开报错。

---

## 未受影响的 API（保持不变）

以下用法两个版本一致，升级无需改动：

- `Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": n}))`
- `w3.is_connected()`
- `w3.eth.contract(address=..., abi=...)`
- `contract.functions.foo(args).call()` — view 调用
- `w3.eth.get_transaction_count(addr, "pending")`
- `w3.eth.account.from_key(pk)` / `w3.eth.account.sign_transaction(tx, pk)`
- `w3.eth.wait_for_transaction_receipt(tx_hash, timeout=n)`
- `web3.exceptions.ContractLogicError` / `TimeExhausted`
- 回执字段 `receipt["status"] / ["gasUsed"] / ["blockNumber"] / ["contractAddress"]`

---

## 其余兼容性风险

| 风险 | 影响 | 处置 |
|---|---|---|
| `matplotlib` 3.9.0→3.9.2、`pandas` 2.2.2→2.2.3 | 同为 patch 升级，API 无变化 | 仅因 cp313 无 wheel 而升，无风险 |
| numpy 被拉到 2.5.1 | pandas 2.2.3 / matplotlib 3.9.2 均已支持 numpy 2.x | 已实测出图正常 |
| **Ganache 依赖 Node.js** | 本机无 Node/npm，私链起不来 | `CHAIN_ENABLED=false` 时走 `NullLedger`，其余全流程照跑；`tests/test_chain_store.py` 自动 skip |
| **FISCO BCOS 依赖 Docker/Linux** | 本机无 Docker，阶段4 无法本地验证 | `LedgerBackend` 协议已留好接口，`get_ledger()` 中有明确的 `NotImplementedError` 提示 |
| 合约 ABI 为手写 | 若修改 `contract.sol` 而忘记同步 ABI，调用会报函数不存在 | 改完 `.sol` 后用 `python deploy_contract.py --save-abi` 自动覆盖 |
| `py-solc-x` 不在 requirements | 仅部署合约需要 | 惰性导入，缺失时给出安装提示，不影响其它模块 |

---

## 回归到 web3 6 的条件

若将来换到 Python ≤3.12 且确实需要 web3 6，把上表 7 处改回驼峰写法即可
（全部集中在 `blockchain/chain_client.py` 与 `deploy_contract.py` 两个文件，
搜索 `# [web3-7]` 可定位）。
