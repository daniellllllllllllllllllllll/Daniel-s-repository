# BlkMaC 实验口径核对与论文材料整理

> **⚠️ 数值已撤下（2026-10-16）**：本文原含各结果目录的具体测量值（正确率、耗时、Gas、
> 负载方差等）。应作者要求，这些**实验数据不再随公开仓库发布**；本地仍保留完整数值
> （`BlkMac_Offline_Files/results*/metrics.csv` 与本文的本地版本）。
> 本文件在此只保留**口径约定、目录可用性判断、方法论与问题清单**，表格中的测量值一律以
> `—` 表示。
>
> 生成方式：只读核对 16 个 `BlkMac_Offline_Files/results*` 目录的 metrics/per_question/trace，
> 以及 `README.md`、`controller/main_controller.py`、`agents/*`、`blockchain/*`、`llm/*` 源码。
> 未读取任何 `.env`，未改动任何源码或结果目录。

---

## 1. 目录清单与统一口径

共 16 个结果目录，按用途归类如下（“正式可用”= 数据自洽且口径一致，可进论文）：

| 目录 | 内容 | 链 | 调度 | 口径状态 |
|---|---|---|---|---|
| `results` | 初始 A/B/C 对照 | 否 | static | ✅ 可用（无链基线） |
| `results_ganache` | A/B/C 对照 + C 真实上链 | C 组是 | static | ✅ **主实验用** |
| `results_adaptive` | A/B/C，C 用自适应调度 | 否 | adaptive | ✅ 调度实验用 |
| `results_trust_aware` | 仅 C，自适应+信任 | 否 | adaptive+trust | ✅ 信任消融用 |
| `results_ablation_mock` | 5 变体无链消融（旧版计时） | 否 | 混合 | ⚠ 框架耗时偏 0，仅正确率/方差可用 |
| `results_ablation_mock_corrected` | 5 变体无链消融（修正计时） | 否 | 混合 | ✅ 无链消融用 |
| `results_ablation_2_1_chain` | 5 变体链上消融 | 是 | 混合 | ❌ 链列残缺（tx 逐变体不全），链上指标不可用 |
| `results_ablation_chain_clean` | 仅 full，链上 | 是 | adaptive+trust | ❌ framework=0（计时 bug） |
| `results_ablation_chain_clean_corrected` | 仅 full，链上，修正计时 | 是 | adaptive+trust | ✅ 参考 |
| `results_ablation_chain_full` | 仅 full，链上 | 是 | adaptive+trust | ❌ 链记录残缺 + 计时 bug |
| `results_ablation_chain_final` | 仅 full，链上 | 是 | adaptive+trust | ⚠ Gas 与主实验不一致，需复核 |
| `results_real_gpt54` | 24 题真实 GPT-5.4 | 否 | static | ❌ 框架≈LLM（计时 bug），弃用 |
| `results_real_gpt54_corrected` | 24 题真实 GPT-5.4（修正） | 否 | static | ✅ 真实模型验证用 |
| `results_real_smoke` | 2 题冒烟 | 否 | static | ❌ 仅接口验证，不可作正式结果 |
| `results_real_smoke2` | 2 题冒烟 | 否 | static | ❌ 同上 |
| `results_real_smoke_corrected` | 2 题冒烟 | 否 | static | ❌ 同上 |

---

## 2. 推荐正式结果版本（论文引用口径）

> 主实验统一为：**24 题、4 Agent、seed=20250728、Mock 后端（模型名「5.6 Luna High」）、
> C 组 static 调度**。真实模型与 Mock 分开展示，不混图、不混表。
>
> 下表只记录**表格结构与口径**；具体测量值见本地结果目录，不随仓库发布。

### 表 1 · 主实验 A/B/C（`results_ganache`，Mock 确定性 + C 组真实上链）

| 组 | 正确率 | 幻觉次数 | 平均耗时(ms) | LLM 耗时(ms) | 框架耗时(ms) | 子任务数 | 负载方差 | 链上交易 | 累计 Gas |
|---|---|---|---|---|---|---|---|---|---|
| A 单 LLM 直答 | — | — | — | — | — | — | — | 0 | 0 |
| B 普通多智能体 | — | — | — | — | — | — | — | 0 | 0 |
| **C 完整 BlkMaC** | — | — | — | — | — | — | — | — | — |

> 定性结论（可公开）：A→B 的正确率提升来自**任务拆解**；B→C 的提升来自**按能力打分派单**
> 使幻觉显著减少。C 组的框架耗时明显高于 A/B，差额来自**链上存证**。

### 表 2 · 上链开销（`results` 无链基线 vs `results_ganache` 带链，同种子 C 组纵向对照）

| C 组 | 正确率/幻觉 | 平均耗时(ms) | LLM(ms) | 框架(ms) | 链上交易 | Gas |
|---|---|---|---|---|---|---|
| 无链基线 | — | — | — | — | 0 | 0 |
| 带链（Ganache） | — | — | — | — | — | — |

> 口径：上链纯开销 = 带链框架耗时 − 无链框架耗时，按题平均；每题交易数 = 链上交易数 / 题数。
> **Gas 逐位确定；框架耗时含网络抖动，应报多次均值/区间，而不是单次值。**

### 表 3 · 调度与负载权衡（正确率 vs 负载方差）

| C 组调度 | 正确率 | 幻觉 | 负载方差 | 来源目录 |
|---|---|---|---|---|
| static（能力打分） | — | — | — | `results` / `results_ganache` |
| adaptive（自适应） | — | — | — | `results_adaptive` |
| adaptive + trust | — | — | — | `results_trust_aware` |

> 规律（可公开）：static 只看能力 → 任务向专家集中（负载方差最高但正确率最高）；
> adaptive 加入负载惩罚 → 更均衡但正确率小幅下降；再加 trust 权重 → 方差回升。
> 这是「准确率 vs 负载均衡」的真实权衡，可直接作为论文讨论点。

### 表 4 · 无链消融（`results_ablation_mock_corrected`，Mock，24 题）

| 变体 | 正确率 | 幻觉 | 平均耗时(ms) | 负载方差 |
|---|---|---|---|---|
| full（完整） | — | — | — | — |
| no_trust（去信任） | — | — | — | — |
| no_adaptive（去自适应） | — | — | — | — |
| no_blockchain（去区块链） | — | — | — | — |
| no_difficulty（去难度匹配） | — | — | — | — |

> ⚠ 使用前须知（见第 3 节问题 2/3）：
> (a) 此处 `full` 是 adaptive+trust+diff，**不等于**主实验的 static「C 完整 BlkMaC」；
> (b) `no_adaptive` 一次改了 scheduling 与 trust 两个开关，不是干净单变量；
> (c) `no_blockchain` 在无链运行下与 full 无差异（都没上链），不能解释为「区块链开销对照」。

### 表 5 · 真实模型验证（`results_real_gpt54_corrected`，OpenAI gpt-5.4，无链，24 题）

| 组 | 正确率 | 幻觉 | 平均耗时(ms) | 子任务数 | 负载方差 |
|---|---|---|---|---|---|
| A 单 LLM | — | — | — | — | — |
| B 普通多智能体 | — | — | — | — | — |
| C 完整 BlkMaC | — | — | — | — | — |

> 定性结论（可公开，且**必须如实写进论文**）：真实模型下 **B/C 未优于 A**，且真实模型的拆解
> 不稳定（子任务数远多于 Mock）。结论：Mock 结果只证明**架构在受控条件下有效**；真实模型下
> 正确率受采样与拆解质量主导，需多次重复报告均值±标准差才能下结论。
> **此表单独成节，不与 Mock 主表合并。**

---

## 3. 已发现问题与风险清单（按严重度）

| # | 问题 | 严重度 | 影响 | 建议 |
|---|---|---|---|---|
| 1 | **「RSA/TCP 加密通信」在所有实验里未被真正运行**：C 组默认 `transport=direct`，全部 trace `tamper_detected=0`、框架耗时≈0.2ms。加密/TCP 只被单测覆盖（test_crypto 41 项、test_tcp_comm 36 项） | 🔴 高 | README 三组表里 C 的「加密通信 ✓」在量化实验中未兑现；论文若照写属失实 | 补跑 `--transport tcp` 的 C 组测加密开销，或在论文中把加密定性为「代码级安全特性（单测验证），不计入 A/B/C 量化对比」 |
| 2 | **消融 `full` ≠ 主实验 `C`**：主实验 C 是 static，消融 full 是 adaptive+trust+diff | 🔴 高 | 两套「完整方法」混用会导致论文自相矛盾 | 统一口径：要么消融以 static C 为 full，要么主实验改用 adaptive；写论文前二选一 |
| 3 | **`no_adaptive` 变体一次改两个变量**（scheduling=static 且 trust_aware=False） | 🟠 中 | 消融不满足单变量原则 | 修正 `build_ablation_variants`：no_adaptive 只把 scheduling 改 static、trust 保持与 full 一致 |
| 4 | `results_ablation_2_1_chain` 链上数据残缺（tx 逐变体不全，Gas 不完整） | 🔴 高 | 链上消融整份不可用 | 弃用该目录链上列；重新跑干净链上消融到新目录 |
| 5 | README「本机实测结果」表的延迟数字与实际 `results_ganache` **不一致** | 🟠 中 | 论文引用延迟数字来源不一 | 以 `results_ganache/metrics.csv` 为准，README 该表需更正或删除 |
| 6 | Gas 在某些目录出现**两个不同值**（差值 ≈ 72 gas/笔 × 交易数） | 🟠 中 | 「Gas 精确确定」的表述可能被打脸 | 重跑时核对合约地址、调度与派单是否一致，确认 canonical 值 |
| 7 | `results_real_gpt54`（未修正）框架≈LLM，计时 bug | 🟡 低 | 已由 `_corrected` 取代 | 只引用 `_corrected`，旧目录标 deprecated |
| 8 | metrics.csv 不记录 `transport` 字段 | 🟡 低 | 无法从产出反推是否走了 TCP | 建议在 `export_results` 的 summary 里加 `transport` 列（改代码前先说明） |

---

## 4. 论文组织方案（章节 ↔ 目录映射）

| 论文章节 | 采用目录 | 图表 |
|---|---|---|
| 实验设计 | 源码 `main_controller.py` + `test_dataset/questions.json` | 三组策略对照表、题库分布表 |
| 主实验 | `results_ganache` | 表 1 + `fig_accuracy_hallucination.png` / `fig_latency.png` |
| 链上存证开销 | `results` vs `results_ganache` | 表 2 + `fig_agent_load.png` |
| 调度与负载 | `results_adaptive` + `results_trust_aware` | 表 3（正确率 vs 方差权衡） |
| 消融 | `results_ablation_mock_corrected`（无链）+ 重跑链上消融 | 表 4 |
| 真实模型验证 | `results_real_gpt54_corrected` | 表 5（单独小节） |
| 安全/加密 | 代码 + 单测（无实验数据） | 定性说明 |

---

## 5. 论文草稿（待用户确认后继续打磨）

### 5.1 实验设计（草稿）

三组共用同一套 24 题题库（含单任务与复合题，覆盖 math/code/logic/knowledge/writing 五领域）、
同一随机种子（20250728）、同一批 4 个专家画像 Agent。差异仅在于：A 组单体 LLM 直接作答；
B 组 LLM 拆解后按固定轮询分配；C 组拆解后按能力打分贪心分配，并将每条交互的 SHA-256 摘要
写入 Ganache 私链。为排除真实模型采样噪声，主实验采用确定性 MockLLM（幻觉概率
`p = 0.18 × (1−competence) × (1+0.25×(difficulty−1))`），使指标差异只能归因于架构。

### 5.2 结果分析（草稿）

- **正确率**：A→B 的增益来自任务拆解（复合题不再整体作答）；B→C 的增益来自按能力派单
  （math 任务交给 math 专家，幻觉次数显著下降）。（具体数值见本地结果目录。）
- **上链开销**：同种子纵向对照下，框架耗时显著上升，差额即纯上链成本；Gas 逐位确定，
  延迟含抖动应报区间。
- **负载权衡**：static 方差最高（任务集中到专家），adaptive 更均衡但正确率小幅下降，
  是「专业对口」与「负载均衡」之间的可量化取舍。

### 5.3 局限性（草稿）

1. MockLLM 是受控模拟，幻觉率与耗时是参数化假设，不代表真实模型行为。
2. 真实 GPT-5.4 验证中 B/C 未优于 A，说明拆解质量与采样噪声在真实模型下占主导。
3. 加密 TCP 链路仅经单元测试验证，其运行时开销未进入主实验量化对比。
4. 链上消融数据残缺，需重跑后方可下结论。
5. 框架耗时含网络抖动，单次运行的延迟数字不应作为精确值引用。

### 5.4 结论（草稿）

BlkMaC 通过「LLM 拆解 + 能力打分派单 + 哈希上链存证」在受控实验中同时提升正确率并大幅减少
幻觉，同时以可量化的链上开销换取全程可审计性；自适应调度提供了正确率与负载均衡之间的可调
权衡。真实模型验证表明架构收益受模型能力与拆解质量约束，需进一步在更大规模与重复实验中验证。

---

## 6. 待办与决策清单（需用户拍板）

- [ ] **P0** 是否补跑 `--transport tcp` 的 C 组（兑现加密表述）→ 新目录 `results_tcp`
- [ ] **P0** 是否重跑干净链上消融 → 新目录 `results_ablation_chain_v2`（替换残缺的 2_1_chain）
- [ ] **P1** 是否修正 `build_ablation_variants` 的 `no_adaptive` 双变量问题（先给 diff）
- [ ] **P1** 确认 Gas canonical 值
- [ ] **P1** 是否在 `export_results` 增加 `transport` 输出列
- [ ] **P2** 更正/删除 README「本机实测结果」表的延迟数字
- [ ] 任何重跑前：确认 Ganache 已启动、RPC/ChainID/私钥/合约地址就位（不读 `.env`）
