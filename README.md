# BlkMaC — 区块链赋能的多智能体协同框架

复现并改良 BlkMaC：Controller 用 LLM 把复杂问题拆成子任务，按**能力打分**动态分配给多个
Task Agent，Agent 间通过 **RSA 加密的 TCP 链路**通信，每条交互记录计算 **SHA-256 摘要上链存证**，
最后跑**三组对照实验**比较正确率、幻觉次数、任务量分布与耗时。

> **【待 Ganache 私有链部署启动后，再进行联调、合约部署测试】**
> 本机未安装 Node.js，Ganache 私链尚未启动。当前 `.env` 中 `CHAIN_ENABLED=false`，
> 链相关代码已按 web3.py 7.16.0 写完但**未经真机联调**；其余全部模块可正常运行。

---

## 快速开始

```powershell
# 1. 安装依赖（6 个包）
.\venv\Scripts\pip.exe install -r requirements.txt

# 2. 准备配置
Copy-Item .env.example .env      # 已存在则跳过

# 3. 跑全部测试（链上用例自动 skip）
.\venv\Scripts\python.exe -m unittest discover -s tests -v

# 4. 跑三组对照实验，出图
.\venv\Scripts\python.exe -m controller.main_controller --mode all --no-chain

# 5. 看产出
Get-ChildItem results
```

无需任何 API Key、无需区块链节点，第 4 步即可直接出完整实验结果。

---

## 目录结构

```
BlkMac_Project/
├── agents/                     # 智能体
│   ├── controller_agent.py     #   拆解 / 打分 / 派单 / 汇总 / 存证
│   └── task_agent.py           #   执行子任务的工作智能体
├── blockchain/                 # 区块链存证（web3.py 7.16.0）
│   ├── chain_client.py         #   Web3 客户端，合约交互核心
│   ├── contract.sol            #   BlkMacLedger 合约源码
│   ├── contract_abi.json       #   合约 ABI
│   └── WEB3_7_MIGRATION.md     #   ★ 与 web3 6.18.0 的 API 差异记录
├── communication/
│   └── tcp_socket.py           # 长度前缀分帧 + RSA/AES 加密 TCP
├── controller/
│   └── main_controller.py      # ★ 三组对照实验主入口
├── crypto/                     # 密码学工具
│   ├── hash_tool.py            #   SHA-256 / 规范化 JSON / Merkle
│   ├── rsa_tool.py             #   RSA-OAEP+AES-GCM 混合加密 / PSS 签名
│   ├── private.key             #   ⚠ 首次运行自动生成，已 gitignore
│   └── public.key
├── llm/                        # 大模型接口
│   ├── llm_base.py             #   抽象基类 + 重试
│   └── llm_api.py              #   MockLLM / OpenAI兼容 / Anthropic + 工厂
├── test_dataset/
│   └── questions.json          # 24 题统一题库（含复合题拆解标注）
├── tests/                      # 单元测试（标准库 unittest）
│   ├── test_crypto.py          #   41 项
│   ├── test_tcp_comm.py        #   36 项
│   ├── test_llm.py             #   48 项
│   ├── test_chain_store.py     #   离线 25 项 + 在线若干（需私链）
│   ├── test_agents.py          #   51 项
│   └── test_experiment.py      #   39 项
├── config.py                   # 统一读取 .env
├── deploy_contract.py          # ⚠ 合约部署，需手动执行
├── .env / .env.example         # 配置（.env 已 gitignore）
└── requirements.txt
```

---

## 三组对照实验

| 组 | 任务拆解 | 分配策略 | 加密通信 | 上链存证 |
|---|---|---|---|---|
| **A** 单 LLM 直答 | ✗ | ✗（单体） | ✗ | ✗ |
| **B** 普通多智能体 | ✓ | 固定轮询 | ✗ | ✗ |
| **C** 完整 BlkMaC | ✓ | **能力打分** | ✓ RSA/TCP | ✓ |

三组跑同一套题库、同一随机种子、同一批 Agent 能力画像，差异只来自上表策略。

```powershell
# 全部三组
python -m controller.main_controller --mode all --no-chain

# 只跑 C 组，走完整 RSA 加密 TCP 链路（慢很多）
python -m controller.main_controller --mode c --transport tcp --no-chain

# 换 Agent 数量与种子
python -m controller.main_controller --mode all --agents 6 --seed 1234 --no-chain

# 快速冒烟（前 5 题、不出图）
python -m controller.main_controller --mode all --limit 5 --no-plot --no-chain

# 使用阶段 1.1（2）的自适应调度，仅跑 C 组
python -m controller.main_controller --mode c --scheduling adaptive --no-chain

# 使用阶段 3.3 的 Adaptive Trust-aware Scheduling
python -m controller.main_controller --mode c --scheduling adaptive --trust-aware --no-chain
```

### 自适应任务调度

`--scheduling adaptive` 在现有能力评分之外，引入 Agent 的历史成功率、质量错误率、
平均响应时间和任务难度匹配度。其评分形式为：

```text
score = w_cap × capability
      + w_success × success_rate
      + w_difficulty_match × difficulty_match
      + w_trust × trust_score
      - w_load × load_penalty
      - w_error × error_rate
      - w_latency × latency_penalty
```

其中错误率包含执行失败和已识别幻觉；响应时间和错误率会随着每个子任务完成而更新，
并影响后续任务分配。启用 `--trust-aware` 后，Trust Score 也参与评分；每次子任务完成后，
成功、幻觉和执行失败分别按 `TRUST_SUCCESS_REWARD`、`TRUST_HALLUCINATION_PENALTY` 和
`TRUST_FAILURE_PENALTY` 更新信誉，最终值限制在 0~1。每个 Agent 的更新次数、最近事件和当前
分数会写入 `agent_load.csv`，更新明细保存在运行时的 `TaskAgent.trust_history` 中。
默认 `static` 模式保留原有 C 组行为，便于与自适应模式做消融对比。
评分分解会写入 `trace.jsonl` 的 `assignments[].score_breakdown`，可用于论文分析。

**产出**（`results/`）：`metrics.csv`、`per_question.csv`、`agent_load.csv`、`trace.jsonl`，
以及三张图 `fig_accuracy_hallucination.png`、`fig_agent_load.png`、`fig_latency.png`。

### 本机实测结果（24 题，4 Agent，seed=20250728，mock 后端，**C 组真实上链**）

| 组别 | 正确率 | 幻觉次数 | 平均耗时 | 其中 LLM | 其中框架 | 子任务数 | 链上交易 | 累计 gas |
|---|---|---|---|---|---|---|---|---|
| A 单 LLM | 79.2% | 5 | 235.16 ms | 235.13 | 0.03 | 24 | 0 | 0 |
| B 普通多智能体 | 87.5% | 5 | 457.17 ms | 456.98 | 0.18 | 39 | 0 | 0 |
| **C 完整 BlkMaC** | **100.0%** | **1** | 458.13 ms | 384.05 | **74.08** | 39 | **39** | 9,182,139 |

运行环境：Ganache v7.9.2（`--deterministic --port 8545 --chain.chainId 1337`），
合约 `BlkMacLedger` 部署于 `0xe78A0F7E…6a8Ab`，39 条 Agent 交互记录全部成功存证。

#### 上链开销：同条件三跑对照

上链成本不是横向从 B 组推断的，而是**同一组 C 在同种子、同题库、同 Agent 画像下
只切换 `--chain` / `--no-chain`** 得到的纵向对照——唯一变量就是上链：

| C 组指标 | 无链基线 | 带链 第 1 次 | 带链 第 2 次 |
|---|---|---|---|
| 正确率 / 幻觉 | 100.0% / 1 | 100.0% / 1 | 100.0% / 1 |
| LLM 耗时 | 384.05 ms | 384.05 ms | 384.05 ms |
| **框架耗时** | **0.20 ms** | **75.86 ms** | **74.08 ms** |
| 总耗时 | 384.25 ms | 459.92 ms | 458.13 ms |
| 负载方差 | 22.1875 | 22.1875 | 22.1875 |
| 链上交易 / gas | 0 / 0 | 39 / 9,182,139 | 39 / 9,182,139 |

**几点可直接写进论文的观察**：

- **正确率 79.2% → 87.5% → 100%**。A→B 的增益来自任务拆解（复合题不再一口气答）；
  B→C 的增益来自按能力派单（math 任务给 math 专家，幻觉从 5 次降到 1 次）。
- **确定性成立**：三次运行的 LLM 耗时都是 384.05 ms，正确率、幻觉数、负载方差逐位相同，
  gas 总量精确到个位重合。因此图表上的任何差异**都不可能来自采样噪声**——
  这是对照实验能成立的前提。
- **上链开销**：框架耗时 0.20 → 74.08 ms，纯上链成本 **73.9 ms/题**。平均每题
  39/24 ≈ 1.6 笔交易，折合**单笔上链约 45.5 ms、约 235.4k gas**，
  使端到端延迟增加 **19.2%**（384.25 → 458.13 ms）。
  这是「可审计性」的直接代价，用 `storeBatch` 批量存证可显著摊薄。
- **gas 完全确定，延迟不完全确定**：两次带链跑的 gas 逐位相同（由 EVM 指令数决定），
  而框架耗时相差 2.4%（75.86 vs 74.08 ms，含网络与节点调度抖动）。
  **论文中 gas 可作为精确成本指标报告，延迟应给多次均值或区间。**
- **一个需谨慎表述的巧合**：C 组总耗时只比 B 组多 0.2%（458.13 vs 457.17 ms），
  看似「上链几乎免费」。真实原因是 C 把子任务派给了更擅长的 Agent，MockLLM 的模拟耗时
  随能力提高而下降（384.05 vs 456.98 ms），**恰好抵消了 74 ms 的上链开销**。
  应当报告的是上链本身的 **19.2%**，抵消关系只作附带讨论——它依赖 MockLLM 的耗时模型，
  换真实模型后必须重新测量。
- **部署链路可完全复现**：重启 Ganache 后重新部署，得到的合约地址与字节码
  （`0xe78A0F7E…6a8Ab`，4059 字节）与上一次逐字节相同。因为合约地址由
  `keccak(rlp(sender, nonce))` 推导，`--deterministic` 固定账户、新链把 nonce 归零，
  加上 solc 可复现编译，三者叠加使整条部署链路可复现。

**关于任务量方差的如实说明**：C 组方差 **高于** B 组。轮询分配天然绝对均匀（方差趋近 0）
但完全不看领域；动态打分为了「专业对口」会把任务集中到专家 Agent 上。当前权重
`w_cap=0.5 > w_load=0.3`，取舍偏向准确率。这正是**阶段4「优化评估函数，进一步平衡负载」**
要解决的问题——提高 `.env` 里的 `SCORE_W_LOAD`，或启用 `SCORE_W_DIFFICULTY`，即可重新取舍。

---

## LLM 后端切换

改 `.env` 的 `LLM_PROVIDER` 即可，代码无需改动：

| provider | 说明 | 需要的配置 |
|---|---|---|
| `mock` | **默认**。确定性模拟模型，零成本、可复现 | 无 |
| `openai` | 任何 OpenAI 兼容端点（OpenAI / DeepSeek / vLLM / Ollama） | `LLM_BASE_URL` + `LLM_API_KEY` |
| `anthropic` | Anthropic `/v1/messages` | `LLM_BASE_URL` + `LLM_API_KEY` |

### 为什么默认用 MockLLM

对照实验要比较**架构**优劣。接真实模型时，每次运行的正确率会因采样随机性漂移几个百分点，
读者无从判断差异来自架构还是噪声。MockLLM 用固定种子把「模型能力」这一变量钉死，
使图表上的差异只可能来自任务分配策略。其幻觉概率模型为：

```
p = base_rate × (1 - competence) × (1 + 0.25 × (difficulty - 1))
```

`competence` 是执行者在该领域的能力值——B 组轮询派单常把 math 任务给不擅长 math 的 Agent
（competence 低 → 幻觉多），C 组按能力派单（competence 高 → 幻觉少）。

> 这是**受控模拟**，不是真实模型行为，论文写作时须说明。
> 把 `LLM_PROVIDER` 换成 `openai`/`anthropic` 即可用真实模型复跑同一套流程。

### 阶段 1：真实 LLM 实验

OpenAI、DeepSeek、Qwen 和 Llama 的兼容接口统一使用 `openai` provider，
只需替换 `LLM_BASE_URL`、`LLM_API_KEY` 和 `LLM_MODEL`：

| 模型 | `LLM_BASE_URL` 示例 | `LLM_MODEL` 示例 |
|---|---|---|
| GPT | `https://api.openai.com` | `gpt-4o-mini` |
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` |
| Qwen | `https://dashscope.aliyuncs.com/compatible-mode` | `qwen-plus` |
| Llama / vLLM / Ollama | 对应服务的 OpenAI 兼容地址 | 服务端实际模型名 |

也可以不修改 `.env`，通过命令行覆盖地址和模型：

```powershell
# 先用 2 题验证真实接口，API Key 仍只从 .env 的 LLM_API_KEY 读取
python -m controller.main_controller --mode all --provider openai `
  --model gpt-4o-mini --base-url https://api.openai.com `
  --limit 2 --no-chain --no-plot --out results_real_gpt

# 验证通过后再跑完整题库
python -m controller.main_controller --mode all --provider openai `
  --model gpt-4o-mini --base-url https://api.openai.com `
  --no-chain --out results_real_gpt
```

真实模型实验会产生 API 调用费用，且题目与回答会发送到所配置的服务商；
运行前应确认服务商、计费账户和数据隐私策略。真实模型结果不保证像 MockLLM 一样确定，
建议每个模型重复运行多次并报告均值、标准差和失败请求数。

---

## 区块链联调（待私链就绪后执行）

```powershell
# 1. 装 Node.js，然后装 Ganache
npm install -g ganache

# 2. 启动私链（--deterministic 保证账户固定，便于复现）
ganache --deterministic --port 8545 --chain.chainId 1337
#    也可用 Foundry 的 anvil：anvil --port 8545

# 3. 把私链输出的第一个账户私钥填入 .env
#    CHAIN_PRIVATE_KEY=0x...

# 4. 安装 Solidity 编译器绑定（只有部署这一步需要，故不在 requirements.txt）
pip install py-solc-x

# 5. 部署合约（会发起真实链上交易，需手动执行）
python deploy_contract.py

# 6. 把输出的合约地址填入 .env
#    CONTRACT_ADDRESS=0x...
#    CHAIN_ENABLED=true

# 7. 跑链上测试
python -m unittest tests.test_chain_store -v

# 8. 跑带上链的完整实验组 C
python -m controller.main_controller --mode all --chain
```

> **⚠ 第 2 步之后请另开一个终端窗口执行第 3–8 步。**
> `ganache` 是**前台阻塞进程**，它占着那个窗口且没有命令提示符。在它里面敲命令，
> 文字只会进 ganache 的 stdin，它既不执行也不报错——现象就是**「输了命令没反应」**。
> 判断方法：可用的窗口提示符形如 `PS C:\...\BlkMac_Project>`；
> ganache 窗口最后一行是 `RPC Listening on 127.0.0.1:8545`，**没有 `PS ...>`**。
>
> 若怀疑部署到底有没有执行，用只读方式查证（`block_number` 为 0 即一笔交易都没发生过）：
>
> ```powershell
> .\venv\Scripts\python.exe verify_chain_readonly.py
> ```

启动 Ganache 时若出现 `Failed to load native module ... µWS` 告警，属正常：
7.9.2 的原生 µWebSockets 二进制不支持 Node 24，会自动回退到纯 JS 实现，功能完整。

**合约接口**（`blockchain/contract.sol`，合约名 `BlkMacLedger`）：

| 函数 | 用途 |
|---|---|
| `storeRecord(bytes32 digest, string taskId, string sender, string receiver)` | 存证一条交互记录 |
| `storeBatch(bytes32[] digests, ...)` | 批量存证，摊薄 gas 与延迟 |
| `verifyDigest(bytes32 digest) → bool` | **防篡改校验入口** |
| `getRecord(uint256 index)` | 读回记录 |
| `getTaskRecords(string taskId) → uint256[]` | 按任务回溯完整交互链 |
| `recordCount() → uint256` | 记录总数 |

设计要点：**只上链 32 字节摘要，不上链明文**——省 gas、不泄露任务内容，
而防篡改只需摘要即可完成校验。合约拒绝重复摘要，天然防重放。

---

## 安全设计

### 加密链路（发送方 → 接收方）

1. 对 `canonical_json(payload)` 计算 SHA-256 摘要
2. 用**自己的私钥** PSS 签名
3. 用**对方的公钥** RSA-OAEP + AES-256-GCM 混合加密
4. 接收方：解密 → 验签 → 校验摘要，任一步失败即抛 `TamperDetected`

**为什么是混合加密**：RSA-OAEP 单次明文上限 = 密钥字节数 − 2×哈希长度 − 2，
2048 位密钥配 SHA-256 只有 190 字节。子任务描述、LLM 回答动辄上千字符，
裸 RSA 会直接 `ValueError: Plaintext is too long`。

**为什么必须长度前缀分帧**：TCP 是字节流，没有消息边界。直接 `sock.recv()`
在连发多条消息时必然粘包。`tcp_socket.py` 用 4 字节大端长度前缀解决。

**为什么所有摘要都要走 `canonical_json`**：Python 字典无序，同一条消息两次
`json.dumps` 键序可能不同，哈希自然不同，链上校验就会出现「数据没被改却验不过」的假阳性。

### 密钥管理

- 所有凭证（私钥、API Key、合约地址）**统一从 `.env` 读取**，代码中无任何硬编码
- `.env`、`crypto/*.key` 已在 `.gitignore` 中忽略
- `crypto/private.key` 首次运行时自动生成，不随仓库分发
- Agent 密钥**惰性生成**：`direct` 模式不走加密链路，不付 RSA 生成开销

---

## 阶段4 改良点位置

| 改良项 | 落点 | 现状 |
|---|---|---|
| FISCO BCOS 轻量链 | `blockchain/chain_client.py` 的 `LedgerBackend` 协议 + `get_ledger()` | 接口已留，`LEDGER_BACKEND=fisco` 时抛 `NotImplementedError` 并给出接入说明 |
| 混合大小模型协同 | `llm/llm_api.py::get_llm(tier="small"/"large")` | 工厂已支持，`.env` 有 `LLM_MODEL` / `LLM_MODEL_SMALL` |
| 难度权重评估函数 | `controller_agent.score()` 的 `- w_diff × difficulty_penalty` 项 | 已实现，`SCORE_W_DIFFICULTY=0.0` 默认关闭 |
| 行业场景落地 | `ControllerAgent` 的 system prompt + Agent `capabilities` 配置 | 均已参数化 |

---

## 兼容性风险与已知限制

| 项 | 说明 |
|---|---|
| **web3 6.18.0 → 7.16.0** | 强制升级：web3 6.x 的依赖 `ckzg`/`lru-dict` 在 Python 3.13 无 wheel 且本机无 MSVC。含 7 处破坏性 API 变更，详见 [`blockchain/WEB3_7_MIGRATION.md`](blockchain/WEB3_7_MIGRATION.md) |
| **matplotlib 3.9.0 → 3.9.2** | 3.9.0 无 cp313 wheel。patch 升级，API 无变化 |
| **pandas 2.2.2 → 2.2.3** | 同上 |
| **Ganache 7.9.2 + Node 24** | ganache 的原生 µWS 二进制不支持 Node 24，启动时告警并自动回退到纯 JS 实现。功能正常，性能有损，本地实验无影响 |
| **Ganache 重启清空状态** | 链上数据全在内存。窗口一关，`CONTRACT_ADDRESS` 立即失效，须重新 `deploy_contract.py` 并回填。`chain_client.py` 会检测到「地址上无合约代码」并给出该提示 |
| **`CHAIN_ENABLED=false` 时** | 走 `NullLedger`，返回 `status=skipped`，不伪装成已上链 |
| **FISCO BCOS** | 需 Docker/Linux，本机无法验证，仅留接口 |
| **`crypto/` 与 `Crypto` 大小写** | 本地包目录与 pycryptodome 顶层包仅差大小写。Python 在 Windows 上导入时做大小写校验，正常不冲突；若设了 `PYTHONCASEOK` 会冲突。`rsa_tool.py` 已加 `Crypto`/`Cryptodome` 双路径导入 |
| **`SO_REUSEADDR` 平台差异** | Windows 上它允许**抢占正在监听的端口**，Unix 不允许。`tcp_socket.py` 在 Windows 改用 `SO_EXCLUSIVEADDRUSE`，使两边行为一致 |
| **MockLLM 是受控模拟** | 幻觉率、耗时均为模拟值，不代表真实模型表现。论文须说明 |
| **`py-solc-x` 不在 requirements** | 仅部署合约需要，惰性导入，缺失时给出安装提示 |

---

## 测试

```powershell
# 全部
python -m unittest discover -s tests -v

# 单个模块
python -m unittest tests.test_crypto -v
python -m unittest tests.test_tcp_comm -v
python -m unittest tests.test_llm -v
python -m unittest tests.test_agents -v
python -m unittest tests.test_experiment -v
python -m unittest tests.test_chain_store -v   # 在线用例需私链，否则自动 skip
```

链相关在线用例的启用条件：`.env` 中 `CHAIN_ENABLED=true` **且** RPC 实际可达。
`CHAIN_ENABLED=false` 时**完全不发起网络探测**，不会在没有节点的机器上白等超时。
