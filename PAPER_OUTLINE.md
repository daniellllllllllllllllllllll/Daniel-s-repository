# 论文编写大纲（PAPER OUTLINE）

> 状态：论文写作思路文档，配套 `PROPOSAL_CORRELATION.md`（方案）与 `EXPERIMENT_RECONCILIATION.md`（已有结果口径）。
> 本文件只做写作规划，不含最终正文；所有结论基于「主推方向已落地」的前提。

---

## 1. 核心叙事线（一句话）

> 在多智能体协作中，同质 Agent 会一起犯错；把「错误相关性」纳入可靠性估计与预算内验证调度，
> 能以更低成本降低错误答案被接受的概率——并在确定性故障注入平台上可复现地证明这一点。

论文骑在**两层贡献**上，两者互相成就：

| 层次 | 贡献 | 来源 |
|---|---|---|
| **方法论层** | 确定性故障注入 + 三组对照 + 诚实消融的**评估方法论** | 仓库已有 MockLLM + `results*` 体系 |
| **算法层** | per-domain Beta + 错误相关性 + 预算内相关性感知验证 | `PROPOSAL_CORRELATION.md` 的 C1/C2/C3 |

**关键策略**：不要只写「我们提了个调度算法」，要写成「我们提了个算法，**并且**在确定性故障注入平台上
证明了差异只来自算法本身、不来自采样噪声」——这正是现有仓库最不可替代、且能直接回应审稿人
「你凭什么说差异不是噪声」的优势。

---

## 2. 建议题目

- 主推（通用算法类）：**Correlation-Aware Budgeted Verification for Reliable LLM Multi-Agent Collaboration**
- 若保留区块链卖点：**Blockchain-Audited Multi-Agent Collaboration with Correlation-Aware Budgeted Verification**
- 中文：**面向可靠 LLM 多智能体协作的相关错误感知预算验证机制**

---

## 3. 章节结构（映射到仓库资产）

| 章节 | 内容 | 对应资产 |
|---|---|---|
| 1. Introduction | 动机：同质 Agent 一起错、全量验证太贵 | — |
| 2. Related Work | 多Agent调度 / diversity ensemble / 成本路由 / 区块链审计 | G-Designer、BAMAS、MasRouter、AgentDropout 等 |
| 3. System Model | Agent、任务、错误相关性 `ρ_ij`、Beta 可靠性、成本模型 | `PROPOSAL` §1 的 C1/C2 形式化 |
| 4. Proposed Method | ① per-domain Beta ② 相关性估计 ③ 冗余调度 + 去相关聚合 | `PROPOSAL` §2 的 D1–D4 |
| 5. Implementation | 基于 BlkMaC 落地，改动清单 | `PROPOSAL` §2 的 D5–D7 |
| 6. Evaluation | 实验矩阵 + 正确率—预算曲线 | 新目录 `results_correlation` |
| 7. Discussion & Limitations | 诚实条款 | 见 §6 |
| 8. Conclusion | — | — |

---

## 4. 贡献声明（Contributions，三点）

```
Contributions:
(1) 提出 per-domain Beta 可靠性后验，替换现有标量信誉，
    使"领域内可靠、领域外不可靠"可被区分；
(2) 提出 Agent 间 per-domain 错误相关性估计
    （能力向量相似度冷启动 + 历史 Pearson 热更新）；
(3) 提出预算约束下的相关性感知冗余调度与去相关加权聚合，
    并在确定性故障注入平台上证明其以更低成本降低错误接受率。
```

> 三个贡献**围绕同一条链**（估计 → 相关性 → 决策），不是三个独立模块——
> 这正是 GPT 反复强调的「不要做三个彼此独立的模块」。

---

## 5. 区块链的定位（必须「降格重定位」）

### 5.1 直接结论

**要提，但把区块链从「核心创新」降为「可审计基质 + 成本维度 + 差异化标签」，并且必须诚实写一句
「本算法本身不依赖区块链」。**

理由（正是 GPT 那个尖锐问题的答案）：
- GPT 问「集中式 Controller 下为什么要区块链？」——**如果不说清，审稿人一定会问同样的问题。**
- 你的相关性/可靠性估计是**建立在历史执行记录上的**；如果这些记录（验证决策、可靠性更新）**防篡改地**
  上链，那么「历史可信」就有据可依。这是区块链唯一站得住的、诚实的角色。

### 5.2 区块链的三个「正当」角色

| 角色 | 写法 | 强度 |
|---|---|---|
| **① 审计基质** | 把「验证决策 + Beta 更新 + 相关性更新」的摘要上链，使可靠性估计的历史**防篡改可追溯**（对应「抗信誉操纵」） | ★★★ 最强、最诚实 |
| **② 成本维度** | 论文主题是 cost-aware；区块链给一个**可量化的审计成本**（gas/笔），顺势写成「可审计性的代价是 X gas」 | ★★★ 契合主题 |
| **③ 差异化标签** | 回答「你和 diversity ensemble 有啥不同」时，补一句「我们的历史是可审计、可追责的」 | ★★ 辅助 |

### 5.3 必须避免的写法

1. ❌ 不声称「区块链提高了正确率」（加密/上链不改变 LLM 推理结果，已有数据也证明 B/C 正确率差异来自调度而非上链）；
2. ❌ 不在集中式单 Controller 场景硬编「跨组织不信任」故事（除非真做了跨组织场景）；
3. ❌ 不把 Blockchain 放标题最前（除非投区块链/安全类期刊）。

### 5.4 可直接用的「区块链定位段落」模板

> *"Note that the proposed scheduling and verification algorithm does not rely on the blockchain for its
> correctness. Rather, the blockchain serves as a tamper-evident substrate: every verification decision and
> per-domain reliability update is committed as a digest, so that the correlation and reliability estimates are
> grounded in an auditable history. This audibility, not any accuracy gain, is the role of the blockchain in our
> system, and its cost is reported explicitly (X gas per record)."*

### 5.5 按投稿目标的写法选择

| 投稿目标 | 区块链写法 |
|---|---|
| 通用 SCI / 算法类期刊 | 作为**系统组件 + 审计模块**，1 个小节 + Limitations 一句「算法不依赖链」 |
| 区块链/安全类（Computer Networks、J. Cloud Computing 等） | 区块链升为核心卖点，但**必须补跨组织信任模型**，否则站不住 |

---

## 6. 必须写进 Limitations 的诚实条款（来自仓库已知问题）

1. MockLLM 是**受控模拟**，幻觉率/相关性是参数化假设，不代表真实模型行为；
2. 真实 GPT 验证（`results_real_gpt54_corrected`）显示 B/C 未优于 A——本方法的结论**必须在真实模型上重复多次**才可外推；
3. RSA/TCP 加密是代码级特性、单测验证，**未进入量化对比**；
4. 相关性估计在小样本下依赖冷启动先验，需报告估计误差。

---

## 7. Related Work 差异化写法

| 你要对比的 | 你的差异化话术 |
|---|---|
| diversity-aware ensemble | 它是静态选模型；你是**预算内动态选择验证对象** |
| G-Designer / BAMAS | 它们优化协作拓扑；你**保持单 Agent 派单、只在风险点加验证** |
| 论文 §3.2 Eval | 标量能力滚动平均 → 你的 **per-domain Beta + 相关性** |
| 现有 BlkMaC | 无冗余、无相关性 → 你的**增量扩展** |

---

## 8. 一句话总结

> 落地后的论文 = **「相关性感知预算验证」的算法贡献** × **「确定性故障注入 + 诚实对照」的方法论贡献**；
> 区块链**从主角降为审计基质**，诚实交代「算法不依赖链、链只提供可审计历史 + 可量化成本」。
