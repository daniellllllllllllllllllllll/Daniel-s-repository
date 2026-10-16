# 相关错误感知的可靠调度与验证 —— 落地方案

> 状态：方案设计文档（尚未实现）。本文只做设计说明，不修改源码；文中行号均指向现有文件真实位置。
>
> 定位：在「确定性故障注入 + 区块链可审计」的多智能体框架上，用 **per-domain 贝叶斯可靠性 + Agent 间错误相关性** 修正调度与聚合，在有限验证预算下降低「错误答案被接受」的概率。

---

## 0. 一句话定位

> 本方案 = GPT 方案 **A1（错误相关性边际收益）** 收窄版 + **B（不可信协作）** 的聚合思想，
> 但 **不引入任务 DAG、不引入工业数据、不做在线重规划**，只加一层「风险感知的冗余验证」。

---

## 1. 创新点声明（Contribution Statement）

### 核心命题（可证伪）

**C1（估计层）**：把现有**标量信任分**（`agents/task_agent.py:182 update_trust`）升级为
**per-domain Beta 后验** `Rel_i(d) = αᵢᵈ/(αᵢᵈ+βᵢᵈ)`，使「Agent 在 math 可靠、在 code 不可靠」
这类领域差异可被区分（现有标量 trust 无法表达）。

**C2（相关性层）**：估计 **Agent 间 per-domain 错误相关性** `ρ_ij^(d)`。冷启动用能力向量相似度
作先验；运行中从历史错误指示向量在线估计。→ 直接回答 GPT 提的核心问题：
*「A₁、A₂ 用同质能力会一起错，不能当两份独立证据」*。

**C3（决策层）**：**预算约束下的相关性感知冗余调度**——在 `B` 次额外 LLM 调用预算内，
贪心选择「预期损失降幅 / 成本」最大的子任务加第二/第三意见，其中边际收益**按相关性打折**
（加一个与现有 Agent 高度相关的 Agent，边际收益低）。聚合用「去相关加权投票」。

### 与已有工作的边界（论文 Related Work 需写清）

| 已有工作 | 差异点 |
|---|---|
| Diversity-aware ensemble / 模型集成 | 它们是「静态选模型」，我们是「**预算内动态决定对哪个子任务加冗余**」 |
| G-Designer / BAMAS（拓扑+预算） | 它们是「选协作结构」，我们是「**保持单 Agent 派单，只在风险点加验证**」，且显式建模**错误相关性**而非仅个体能力 |
| 论文 §3.2 的 Eval | 论文是「标量能力滚动平均」，我们是「per-domain Beta + 相关性」 |
| 现有 BlkMaC | 现有是「一个子任务一个 Agent、无冗余、无相关性」，本方案是**增量扩展** |

### 明确不作为创新点的（防审稿人攻击）

- Beta 信誉本身（成熟贝叶斯）；Pearson 相关性本身（成熟统计）；贪心预算分配本身（成熟）。
- **真正的 novelty = 三者在「确定性故障注入 + 可审计 MAS」下的联合 + 相关性打折的边际收益，
  并以可复现对照实验证明。**

---

## 2. 改动 diff 级说明

### D1 · `agents/task_agent.py` — per-domain Beta 可靠性

**位置**：`__init__` 统计量初始化（现有 139–150 行附近）

```diff
         self.trust_score = min(1.0, max(0.0, settings.TRUST_INITIAL_SCORE))
         self.trust_updates = 0
         self.trust_last_event = "initial"
         self.trust_history: list[dict[str, Any]] = []
         self.results: list[AgentResult] = []
+
+        # per-domain Beta 可靠性后验（Beta 先验 α=β=1 → 均匀）
+        self.alpha = {d: 1.0 for d in DOMAINS}
+        self.beta  = {d: 1.0 for d in DOMAINS}
+        self.error_flags: dict[str, list[int]] = {d: [] for d in DOMAINS}
```

**位置**：新增方法，放在 `update_trust`（182 行）之后

```diff
+    def update_reliability(self, result: AgentResult) -> None:
+        """按领域更新 Beta 后验；错误/幻觉记 beta，成功记 alpha。"""
+        d = result.domain
+        if (not result.ok) or result.hallucinated:
+            self.beta[d] += 1.0
+        else:
+            self.alpha[d] += 1.0
+        self.error_flags[d].append(0 if result.ok and not result.hallucinated else 1)
+
+    def reliability(self, domain: str) -> float:
+        """后验成功率 = α/(α+β)。"""
+        return self.alpha[domain] / (self.alpha[domain] + self.beta[domain])
```

**位置**：`execute()` 内 `self.update_trust(result)`（294 行）之后

```diff
             self.update_trust(result)
+            self.update_reliability(result)
             self.results.append(result)
```

**位置**：`reset()`（357 行）与 `stats()`（341 行）各补一处（重置后验 / 输出后验）

```diff
     def reset(self) -> None:
         ...
             self.trust_history.clear()
             self.results.clear()
+            self.alpha = {d: 1.0 for d in DOMAINS}
+            self.beta  = {d: 1.0 for d in DOMAINS}
+            self.error_flags = {d: [] for d in DOMAINS}
         self.llm.reset_stats()
```

```diff
     def stats(self) -> dict:
         return {
             ...
             "capabilities": dict(self.capabilities),
+            "reliability": {d: round(self.reliability(d), 4) for d in DOMAINS},
         }
```

---

### D2 · `agents/task_agent.py` — 错误相关性估计

**位置**：新增方法（`reliability` 之后）

```diff
+    def error_correlation(self, other: "TaskAgent", domain: str) -> float:
+        """per-domain 错误相关性。冷启动用能力向量相似度先验，热启动用 Pearson。"""
+        a, b = self.error_flags[domain], other.error_flags[domain]
+        n = min(len(a), len(b))
+        if n < settings.CORRELATION_MIN_SAMPLES:      # 冷启动：能力向量相似度
+            dot = sum(self.capabilities[d] * other.capabilities[d] for d in DOMAINS)
+            na  = sum(v * v for v in self.capabilities.values()) ** 0.5
+            nb  = sum(v * v for v in other.capabilities.values()) ** 0.5
+            return max(0.0, min(1.0, dot / (na * nb + 1e-9)))
+        # 热启动：最近 n 个错误指示向量的 Pearson 相关
+        import statistics
+        x, y = a[-n:], b[-n:]
+        return max(-1.0, min(1.0, statistics.correlation(x, y)))
```

> 冷启动先验是关键设计：`_CAPABILITY_PROFILES`（`task_agent.py:384`）里两个 math 都弱的 Agent，
> 能力向量相似度高 → 先验相关性高。**这意味着「相关错误」不需要额外造数据就能从现有画像推出来。**

---

### D3 · `agents/controller_agent.py` — 相关性感知聚合

**位置**：新增方法，放在 `aggregate`（514 行）之后

```diff
+    def aggregate_redundant(self, subtask: SubTask, results: list[AgentResult]) -> str:
+        """去相关加权投票：高度相关的同答案只算一份打折证据。"""
+        agents = {a.agent_id: a for a in self.agents}
+        selected: list[str] = []
+        weights: dict[str, float] = {}
+        for r in sorted(results, key=lambda r: -agents[r.agent_id].reliability(subtask.domain)):
+            a = agents[r.agent_id]
+            corr = max([a.error_correlation(agents[s], subtask.domain)
+                        for s in selected], default=0.0)
+            weights[r.agent_id] = a.reliability(subtask.domain) * (1.0 - corr)
+            selected.append(r.agent_id)
+        from collections import defaultdict
+        score = defaultdict(float)
+        for r in results:
+            score[r.answer] += weights[r.agent_id]
+        return max(score, key=score.get)
```

> 这是「相关性打折」的**最简可解释实现**（不是高深模型），作为 Proposed 第一版；
> 论文里可与「朴素信誉加权（corr=0）」作消融对比，突出相关性项的贡献。

---

### D4 · `agents/controller_agent.py` — 风险感知冗余调度

**位置**：新增方法，放在 `assign`（464 行）之后

```diff
+    def add_redundancy(self, assignments: list[Assignment], budget: int) -> dict[str, list[str]]:
+        """预算内贪心加第二/第三意见。边际收益按相关性打折。"""
+        redundancy: dict[str, list[str]] = {}
+        remaining = budget
+        while remaining > 0:
+            best = None
+            for asg in assignments:
+                cur = redundancy.get(asg.subtask.id, [asg.agent_id])
+                a_cur = self._agent_by_id(cur[-1])
+                p_fail = 1.0 - a_cur.reliability(asg.subtask.domain)
+                for j in self.agents:
+                    if j.agent_id in cur:
+                        continue
+                    corr = max(j.error_correlation(self._agent_by_id(k), asg.subtask.domain) for k in cur)
+                    gain = p_fail * j.reliability(asg.subtask.domain) * (1.0 - corr)
+                    if best is None or gain > best[0]:
+                        best = (gain, asg.subtask.id, j.agent_id)
+            if best is None or best[0] <= 0:
+                break
+            _, tid, jid = best
+            redundancy.setdefault(tid, []).append(jid)
+            remaining -= 1
+        return redundancy
```

**位置**：`run()` 内 `assignments = self.assign(...)`（555 行）之后、`dispatch` 循环（559 行）改造

```diff
         assignments = self.assign(subtasks) if use_scoring else self.assign_round_robin(subtasks)
         trace.assignments = [a.to_dict() for a in assignments]
+
+        redundancy: dict[str, list[str]] = {}
+        if self.redundancy_budget > 0:
+            redundancy = self.add_redundancy(assignments, self.redundancy_budget)
 
         results: list[AgentResult] = []
         for assignment in assignments:
+            extra = redundancy.get(assignment.subtask.id, [])
             try:
                 result = self.dispatch(assignment)
+                extra_results = [self.dispatch(
+                    Assignment(subtask=assignment.subtask, agent_id=jid, score=0.0)
+                ) for jid in extra]
+                if extra_results:
+                    result = AgentResult(
+                        subtask_id=assignment.subtask.id,
+                        agent_id=assignment.agent_id,
+                        answer=self.aggregate_redundant(assignment.subtask, [result] + extra_results),
+                        domain=assignment.subtask.domain,
+                        difficulty=assignment.subtask.difficulty,
+                    )
             except Exception as exc:
                 ...
```

**位置**：`__init__`（170–209 行）加一个成员

```diff
         self.difficulty_matching = difficulty_matching
+        self.redundancy_budget = 0   # 默认关闭；实验时由 CLI 传入
```

---

### D5 · `llm/llm_api.py` — MockLLM 相关错误注入（实验平台核心）

**位置**：`MockLLM._complete`（253 行）的 `salt` 构造

```diff
-        rng = self._rng(prompt, salt=f"{kind}|{competence:.3f}|{difficulty}")
+        error_group = kwargs.get("error_group", None)
+        salt = f"{kind}|{competence:.3f}|{difficulty}"
+        if error_group is not None:
+            salt += f"|g{error_group}"   # 同组 Agent 共享错误潜因子 → 相关性可控
+        rng = self._rng(prompt, salt=salt)
```

**位置**：`agents/task_agent.py::execute`（244 行 `self.llm.generate(...)`）透传

```diff
             response = self.llm.generate(
                 self._build_prompt(subtask),
                 system=self.SYSTEM_PROMPT,
                 kind=TaskKind.ANSWER,
                 competence=self.capability(subtask.domain),
                 difficulty=subtask.difficulty,
+                error_group=getattr(self, "error_group", None),
             )
```

**位置**：`make_agent_pool`（393 行）里按能力画像给 Agent 打 `error_group` 标签（同画像 → 同组 → 相关错误）

```diff
             TaskAgent(
                 agent_id=f"{id_prefix}-{i + 1}",
                 llm=llm_factory(i),
                 capabilities=dict(profile),
                 host=host,
+                # error_group 在 TaskAgent 内用 profile 相似度自动赋，或显式传
             )
```

> 这一步让「相关性」从「统计假设」变成「**确定性、可复现、可调档**」的实验变量——
> 这是本方案区别于所有已有工作的实验优势，也正是仓库「受控模拟」方法论的自然延伸。

---

### D6 · `config.py` + `.env.example` — 新配置项

```diff
+ SCORE_W_CORRELATION       # 相关性修正项权重（默认 0.0，作为消融开关）
+ REDUNDANCY_BUDGET         # 额外 LLM 调用预算（默认 0 = 关闭冗余）
+ CORRELATION_MIN_SAMPLES   # 冷启动转热启动的样本阈值（默认 4）
+ BETA_PRIOR_ALPHA / BETA_PRIOR_BETA   # Beta 先验（默认 1.0/1.0）
```

---

### D7 · 测试

| 文件 | 新增用例 |
|---|---|
| `tests/test_agents.py` | per-domain Beta 更新正确性；`reliability()` 冷/热；`error_correlation()` 冷启动先验 vs 热启动 Pearson |
| `tests/test_experiment.py` | `add_redundancy` 预算耗尽即停；相关性越高边际收益越低；`aggregate_redundant` 去相关投票正确性 |

---

## 3. 实验矩阵

### 3.1 变量与水平

| 变量 | 水平 |
|---|---|
| **错误相关性**（D5 控制） | 独立（异画像）/ 中等相关（2 个同画像）/ 高度相关（全部同画像） |
| **验证预算 B** | 0 / 4 / 8 / 12（额外 LLM 调用） |
| **调度方法** | Baseline 贪心 / **Proposed 相关性感知** |
| Agent 数量 | 4（主）/ 8（可扩展性） |
| 难度 | 复用题库 1–5 |

### 3.2 基线（必须含强基线）

| 基线 | 冗余策略 | 聚合策略 |
|---|---|---|
| B0 无冗余 | 无（= 现有 static C 组） | 单 Agent 直答 |
| B1 随机冗余 | 随机选子任务加 2nd | 多数投票 |
| B2 信誉加权冗余 | 选失败风险最高者加 2nd | **朴素信誉加权**（corr=0，即 Proposed 去掉相关性项） |
| **Proposed** | 相关性感知边际收益贪心 | 去相关加权投票 |

> B2 是**最关键**的基线：它与 Proposed 只差「相关性打折」这一项，能直接隔离出「相关性建模」的贡献——
> 这正是 GPT 强调的「强基线」要求。

### 3.3 指标

| 指标 | 含义 |
|---|---|
| **错误接受率** | 最终聚合答案仍错误的比例（核心指标） |
| 正确率 / 幻觉数 | 与现有口径一致，便于和 `results*` 对比 |
| 额外调用次数 | 成本（= B） |
| **正确率—预算曲线** | 同预算下横向比正确率（论文主图） |
| （可选）链上记录数 | 冗余结果的摘要上链量 |

### 3.4 假设与预期

| 假设 | 预期 |
|---|---|
| H1 | **高相关场景下，Proposed 的错误接受率显著低于 B2**（B2 会把同质 Agent 的相同错误当成两份独立支持） |
| H2 | 相同正确率下，Proposed 所需预算 ≤ B2（边际收益打折避免浪费） |
| H3（稳健性检验） | **独立错误场景下，Proposed ≈ B2**（相关性≈0 时不应造成伤害） |

> H3 是「诚实性」设计：如果独立场景下 Proposed 反而变差，说明相关性建模引入了噪声，需要调
> `SCORE_W_CORRELATION`。这条检验让结论可证伪，而不是「Proposed 处处更好」的漂亮假象。

---

## 4. 落地顺序与工作量估计

| 阶段 | 内容 | 工作量 | 依赖 |
|---|---|---|---|
| P1 | D1（per-domain Beta）+ D6（配置）+ 测试 | 0.5 天 | 无 |
| P2 | D2（相关性）+ 测试 | 0.5 天 | P1 |
| P3 | D5（MockLLM 相关错误注入） | 0.5 天 | 无 |
| P4 | D3 + D4（聚合 + 冗余调度） | 1–1.5 天 | P1/P2/P3 |
| P5 | 跑实验矩阵 + 出图（`results_correlation` 新目录） | 1 天 | P4 |
| P6 | 真实 GPT 小样本交叉验证（`results_correlation_real`） | 0.5 天 | P5 |

**合计约 4–4.5 天**，且**每一步都可单独验证**（P1–P3 是纯增量，不影响现有 A/B/C 结果——
`REDUNDANCY_BUDGET=0` 时行为与现在完全一致）。

---

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| 「这不就是 diversity-aware ensemble」 | novelty 收窄到「**预算内动态选择验证对象 + 相关性打折的边际收益**」，并在 Related Work 显式对比 diversity-aware ensemble 的静态性 |
| 相关性估计在小样本下噪声大 | 冷启动先验兜底 + 消融 `CORRELATION_MIN_SAMPLES`；报告相关性估计误差 |
| 冗余带来额外成本，性价比存疑 | 主图就是「正确率—预算曲线」，用数据说话，而不是宣称「冗余一定好」 |
| MockLLM 结果被质疑 | 沿用现有「受控模拟 + 真实 GPT 交叉验证」双轨，与主实验口径一致 |

---

## 附：与现有已知问题的关系

- 本方案**不碰** `transport=direct/tcp`、gas 两个值等现有未决问题，完全正交。
- 复用 `results_ganache` 的确定性口径（24 题、4 Agent、seed=20250728），新结果写 **`results_correlation`**
  （不覆盖旧目录，符合安全边界）。
- 若最终不采用，D1/D2 的 per-domain Beta 仍可独立保留为「Adaptive Capability Learning」的低成本实现，不浪费。
