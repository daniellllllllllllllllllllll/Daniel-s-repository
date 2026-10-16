# Correlation-Aware Budgeted Verification for Reliable LLM Multi-Agent Collaboration

> **状态**：论文草稿（Introduction / Related Work / System Model / Proposed Method / Evaluation / Limitations）。
> 配套资产：`PROPOSAL_CORRELATION.md`（方案）、`results_correlation/`（本方案实验数据）、
> `BlkMac_Offline_Files/results_ganache`（既有主实验）、`EXPERIMENT_RECONCILIATION.md`（口径核对）。
>
> **使用说明（中文，投稿前删除）**
> 1. 本文所有实验数字都来自本仓库真实运行，**没有任何编造**；凡未完成的实验都在 Limitations 里显式标注。
> 2. 关键诚实条款：**H1 不成立**（高相关场景下 Proposed 的错误接受率并不优于强基线 B2），
>    成立的假设是 **H2（同等正确率下预算显著更低）** 与 **H3（独立场景下不劣于 B2）**。
>    论文叙事必须围绕「**以更低成本达到同等可靠性**」展开，**不能**写成「处处更准」。
> 3. MockLLM 是受控模拟，不得与真实模型结果混算/混图。

---

## 1. Introduction

*(第一段)* Large language model (LLM) multi-agent systems increasingly decompose a complex task into
subtasks, route each subtask to a specialised agent, and aggregate the results. The dominant design
principle is *capability matching*: score each agent against each subtask and dispatch to the best
match. This works well when agents fail independently, because a pool of diverse agents then provides
genuine redundancy. In practice, however, agents built on the same base model, the same prompting
recipe, or the same retrieval corpus tend to fail on *the same* items: they share an error mode. Under
such correlated failures, a second or third opinion is not a second or third piece of evidence. Recent
work on ensemble diversity and multi-agent topology search has made this observation informally, but
the cost of *acting* on it inside a scheduling loop remains unquantified.

*(第二段)* At the same time, verifying every subtask with every agent is unaffordable: verification is
exactly the expensive resource that multi-agent decomposition is meant to save. The practical question
is therefore not "should we verify?" but "*which subtask, with which agent, under a fixed call
budget, given that agents' errors may be correlated?*" This paper answers that question with a
three-part mechanism — a per-domain Bayesian reliability posterior, a per-domain inter-agent error
correlation estimator, and a budgeted, correlation-discounted redundancy scheduler with decorrelated
weighted aggregation — implemented on top of an existing blockchain-audited multi-agent framework
(BlkMaC) whose fault injection is *deterministic*, so that measured differences can be attributed to
the algorithm rather than to sampling noise. Our evaluation is deliberately falsifiable: we state three
hypotheses up front and report that one of them **fails**.

---

## 2. Related Work

**Multi-agent scheduling and topology.** G-Designer, BAMAS, MasRouter and AgentDropout optimise the
*collaboration structure* — which agents talk to which, how many agents to spawn, or which agents to
drop. Our work keeps the existing single-agent-per-subtask dispatch and adds only a *verification
layer* on top of it: we do not search over topologies, and our budget is spent on extra calls for a
small number of risky subtasks rather than on restructuring the graph.

**Diversity-aware ensembles and cost-aware routing.** Diversity-aware ensembling selects a static,
diverse set of models, and cost-aware routing selects a single model per query subject to a price
constraint. Both treat model errors as exchangeable and assume independence when combining votes. We
instead *estimate* the correlation between agent errors online and use it to discount marginal
verification value, so that the budget is spent where an additional opinion is actually informative.

**Reliability estimation in multi-agent frameworks.** The underlying BlkMaC framework maintains a
*scalar* trust score updated by a fixed reward/penalty schedule (§3.2 of the original system). A scalar
trust cannot express "this agent is reliable in mathematics but unreliable in code". We replace it with
a per-domain Beta posterior, which is a small but load-bearing change: every downstream quantity
(marginal value, aggregation weight) becomes domain-specific.

**Blockchain auditing.** We follow the honest positioning argued for in our design notes: the proposed
algorithm does **not** rely on a blockchain for its correctness. The ledger serves as a
tamper-evident substrate for the *history* on which reliability and correlation estimates are built —
verification decisions and posterior updates are committed as digests, so that "the history is
trustworthy" is an auditable claim rather than a procedural assumption. Its cost is reported in gas,
and no accuracy gain is claimed for it.

---

## 3. System Model

**Agents and subtasks.** A pool of `n` agents `A = {a_1, …, a_n}`. A task is decomposed into subtasks
`s ∈ S`, each with a domain `d(s) ∈ D` (here `|D| = 5`) and a difficulty `k(s) ∈ {1,…,5}`. Agent `a_i`
holds a capability vector `c_i ∈ [0,1]^|D|`. By default each subtask is dispatched to one agent by
capability-based scoring.

**Per-domain reliability.** Agent `a_i` has a Beta posterior over its success probability in domain `d`:

```
α_i(d) ← α_i(d) + 1   if the result is correct
β_i(d) ← β_i(d) + 1   otherwise (execution failure or hallucination)
Rel_i(d) = α_i(d) / (α_i(d) + β_i(d)),      prior α = β = 1  (uniform)
```

Because the posterior is per-domain, `Rel_i(math) ≫ Rel_i(code)` is representable, which a scalar trust
score cannot express.

**Error correlation.** For a domain `d`, let `e_i^d ∈ {0,1}^{m}` be the error-indicator history of agent
`a_i` in that domain. We estimate

```
ρ_ij(d) = Pearson(e_i^d, e_j^d)                       if min(m_i, m_j) ≥ τ
ρ_ij(d) = cos(c_i, c_j) ∈ [0,1]                       otherwise (cold start)
```

The cold-start prior uses *capability-vector similarity* deliberately: two agents that are both weak in
mathematics have similar capability vectors and therefore a high prior correlation, so "like-minded
agents err together" is expressible before any joint history exists. This is the design choice that
makes the correlation level of an experiment consistent with what the estimator can see at time zero.

**Cost model.** Verification budget `B` is the number of *additional* LLM calls allowed per task.
Baseline `B = 0` reproduces exactly the existing framework behaviour.

**Metrics.** *Error acceptance rate* = fraction of tasks whose final aggregated answer is wrong
(primary metric). We also report accuracy, hallucination count, and the *realised* number of extra
calls (`redundant_calls`), which is the cost actually paid.

---

## 4. Proposed Method

### 4.1 Budgeted, correlation-discounted redundancy scheduling

Given the primary assignment, each remaining unit of budget is allocated greedily to the (subtask,
verifier) pair with the largest expected reduction in failure probability:

```
gain(s, j) = (1 − Rel_head(s)) · Rel_j(d(s)) · (1 − max(0, ρ_j,chain(s)))
```

Three factors: how likely the current chain still is to fail; how reliable the candidate verifier is;
and whether the verifier merely repeats the same error mode. The chain always begins with the primary
agent. Search stops when the best gain falls below a small tolerance `ε = 1e-9` (see §6 for why this
tolerance is not cosmetic: without it, floating-point noise in `1 − ρ` lets the scheduler spend the
whole budget on verifiers whose information gain is numerically zero).

The correlation is used **only to discount, never to amplify**: we clamp `ρ ← max(0, ρ)` before forming
`1 − ρ`. Negative Pearson values (anti-correlated errors) are real and informative, but allowing
`1 − ρ > 1` would let a verifier outweigh the primary agent and flip correct answers; we observed this
degradation empirically and removed it.

Setting `ρ ≡ 0` in exactly the same code path yields the **strong baseline B2** (reputation-weighted
redundancy), so the contribution of correlation modelling is isolated by a single flag rather than by
comparing across implementations.

### 4.2 Decorrelated weighted aggregation

When a subtask has several answers, agents are considered in decreasing order of posterior reliability
and each receives the weight

```
w_j = Rel_j(d) · (1 − max(0, max_{k already included} ρ_jk))
```

Answers are then voted on with these weights. A second opinion from an agent that is highly correlated
with one already counted is treated as a discounted — not independent — piece of evidence. If all
weights collapse to zero, the rule degrades gracefully to majority vote with a reliability-based tie
break.

### 4.3 Correlation injection for controlled evaluation

To make error correlation an *experimental variable* rather than a statistical hope, the deterministic
mock backend gains an optional latent factor: agents sharing a group label draw their
"hallucinate-or-not" decision — and the particular wrong answer — from a single random stream keyed only
by `(group, task type, difficulty, prompt)`. Crucially, this stream **must not** include the agent's own
seed, because in the framework each agent's seed is `experiment_seed + salt`; including it silently
destroys the shared factor (our manipulation check caught exactly this: measured within-group
correlation was far below 1.0, and rose to essentially 1.0 after the fix). Three levels are produced by
reusing capability profiles: distinct profiles (independent), two profiles used twice (moderate), and a
single profile for all agents (high).

---

## 5. Implementation

Implemented on BlkMaC (tasks: decomposition → capability dispatch → optional encrypted transport →
ledger anchoring → aggregation). Changes:

| Component | Change |
|---|---|
| `agents/task_agent.py` | per-domain Beta posterior, `Rel_i(d)`, `error_correlation`, error-group tag; posteriors/error history are reset with the rest of the statistics |
| `agents/controller_agent.py` | `add_redundancy` (greedy / random), `aggregate_redundant`, redundancy trace, decorrelated merge |
| `llm/llm_api.py` | optional shared error latent factor in the deterministic mock backend |
| `config.py`, `.env.example` | `SCORE_W_CORRELATION`, `REDUNDANCY_BUDGET`, `CORRELATION_MIN_SAMPLES`, `BETA_PRIOR_ALPHA/BETA` |
| `controller/main_controller.py` | method/budget/correlation-level wiring and the new cost & error-acceptance metrics |
| `controller/correlation_experiment.py` | experiment matrix, manipulation check, figures, automatic hypothesis verdicts |

**Backward-compatibility guard.** With `REDUNDANCY_BUDGET = 0` and correlation level `none`, the new
code path is inert. We verify this by re-running the original three-group experiment and comparing
against the archived reference run *field by field*: every deterministic field (question count,
correct count, accuracy, hallucination count, subtask count, load variance) is bit-identical.
The concrete values are kept locally (`BlkMac_Offline_Files/results_ganache/`) and are **not**
published with this repository. The test suite (304 cases) passes; the only differences in the
regenerated files are wall-clock timings and the ledger columns that require a running private chain.

---

## 6. Evaluation

**Setup.** 24-question benchmark, 4 agents, fixed seed 20250728, deterministic mock backend, no chain.
Matrix: correlation level {independent, moderate (pairs), high (all)} × budget `B ∈ {0,4,8,12}` ×
method {B0 none, B1 random, B2 reputation-weighted, Proposed}. 30 runs. The raw per-run data is
kept locally (`results_correlation/`) and is **not** published with this repository.

**Manipulation check (validity of the independent variable).** With all agents answering the same
probe set, the measured within-group error correlation is essentially +1, whereas the cross-group
correlation stays close to zero in both the independent and the moderate level. The three levels
therefore differ in error *structure*, not merely in labelling. (Concrete values are kept locally.)

**Difficulty calibration (disclosed).** Under the framework's default mock hallucination rate, the
full system already saturates these 24 questions — there is no error mass left to reduce, so the
matrix would degenerate. We therefore raise the mock hallucination rate (a *stress-calibration knob*,
recorded in `results_correlation/run_config.json`) so that the baseline has a measurable error space;
every comparison below is conditioned on that stress level.

### 6.1 Headline result: redundant verification buys nothing when errors are correlated

The table of error-acceptance rates and realised call costs per (correlation level × method) is kept
locally (`results_correlation/metrics.csv`) and is **not** published with this repository. The
qualitative pattern is:

* **high correlation** — the baseline's error acceptance rate is *unchanged* by every redundancy
  method, even after spending the full budget. This is the quantitative form of the observation that
  motivates the paper: *when agents share an error mode, more redundancy cannot buy accuracy*.
* **moderate correlation** — all three redundancy methods reduce the error acceptance rate well below
  the baseline, and Proposed reaches the same level as the strong baseline using substantially fewer
  extra calls.
* **independent errors** — redundancy is roughly neutral (see §6.3).

The main figure (`results_correlation/fig_accuracy_budget.png`) shows accuracy against budget for each
correlation level, with the baseline drawn as a horizontal reference.

### 6.2 Hypothesis verdicts

* **H1 (Proposed's error acceptance rate is lower than B2's under high correlation): NOT SUPPORTED.**
  No budget level is strictly better. The reason is structural: with a homogeneous pool, B2's
  extra "opinion" is perfectly correlated with the primary and is therefore *neutral*, not harmful —
  it simply consumes budget. We report this rather than re-framing it.
* **H2 (equal accuracy at no greater budget): SUPPORTED at every correlation level.** Reaching B2's best accuracy costs
  Proposed no more calls than B2 at every correlation level, and strictly fewer at the moderate and
  high levels; in the high-correlation regime Proposed correctly declines to spend at all.
* **H3 (no harm when errors are independent): SUPPORTED.** Proposed's accuracy is never below B2's and
  it tracks the baseline.

### 6.3 Secondary observation (reported, not smoothed over)

In the independent regime the baseline (no redundancy) is slightly *better* than every redundancy
method, including Proposed. With two opinions and equal weights the vote can tie, and the current rule
then keeps the primary agent's answer; adding a vote therefore does
not automatically help. We report this negative result because it bounds the regime in which the
mechanism is useful: budgeted verification pays off when the baseline has a *large* error mass and the
pool contains genuinely uncorrelated competence.

---

## 7. Limitations (honest disclosures)

1. **No accuracy advantage under high correlation.** H1 fails. The demonstrated benefit is cost, not
   accuracy. Any claim that correlation-aware scheduling makes a homogeneous pool more reliable would
   contradict our own data.
2. **Mock backend is a controlled simulation.** Hallucination probabilities and the error latent factor
   are *parameterised assumptions*, not measurements of real models. The high-correlation result is,
   in effect, a stress test of a plausible failure structure.
3. **Stress-calibrated difficulty.** The matrix required raising the mock hallucination rate because
   the default setting saturates the benchmark. Results are conditional on that choice.
4. **Real-model cross-validation is incomplete.** A small-sample run with a real API is planned and its
   output directory (`results_correlation_real`) is reserved, but at the time of writing the configured
   endpoint was unreachable from the execution environment, so no real-model numbers for this mechanism
   are reported. Mock and real results must never be pooled.
5. **Prior real-model finding is unfavourable and must be cited.** In the archived real-model
   experiment (gpt-5.4, `results_real_gpt54_corrected`), neither the full system nor the round-robin
   baseline outperformed the single-LLM baseline — the multi-agent advantage did not reproduce on a
   real model. Nothing in this paper should be read as contradicting that. (Concrete values are kept
   locally and are not published with this repository.)
6. **Encrypted transport is a code-level property.** RSA+AES over TCP is implemented and unit-tested,
   but the quantitative experiments run with `transport=direct`; no latency or accuracy claim is made
   for the encrypted path.
7. **Correlation estimates are noisy in small samples.** The cold-start prior carries the estimator
   early on; we report the manipulation check but not a calibration curve of estimation error versus
   sample size.
8. **Aggregation ties are under-specified.** As noted in §6.3, ties currently resolve to the primary
   agent. A principled tie-breaking rule (e.g. abstention or a third call) is future work.

---

## 8. Conclusion

We presented a correlation-aware, budgeted verification layer for LLM multi-agent collaboration:
per-domain Beta reliability, a per-domain inter-agent error-correlation estimator with a
capability-similarity cold start, and a greedy scheduler whose marginal value is discounted by
correlation, coupled with decorrelated weighted aggregation. On a deterministic fault-injection
platform, with the correlation level itself verified by a manipulation check (+1.0 within group, ≈0
across groups), the mechanism reduces the *cost* of reaching a given reliability — substantially fewer
extra calls at moderate correlation, and none at all at high correlation — while never
under-performing the strong reputation-weighted baseline. We also report, explicitly, that under high
error correlation *no* redundancy method improves accuracy, which is the sharpest available statement
of why correlated errors must be modelled rather than assumed away.
