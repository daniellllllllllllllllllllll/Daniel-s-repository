"""Controller Agent —— BlkMaC 框架的大脑。

职责四件事：

1. **任务拆解**（:meth:`decompose`）：用 LLM 把复杂问题拆成带领域与难度标注的子任务。
2. **能力打分**（:meth:`score`）：对每个「Agent × 子任务」组合算一个匹配分。
3. **动态分配**（:meth:`assign`）：贪心取最高分派单，并**即时更新负载**，
   使后续子任务自动避开已经排满的 Agent。
4. **汇总 + 存证**（:meth:`run`）：收集子结果、逐条上链、用 LLM 汇总成最终答案。

静态打分函数
--------
::

    score = w_cap  * capability[domain]
          + w_load * (1 - load / max_load)
          + w_succ * success_rate
          - w_diff * difficulty_penalty

前三项分别对应「专业对口」「负载均衡」「历史可靠」。
第四项 ``w_diff`` **默认为 0**，是阶段4「优化评估函数（增加任务难度权重）」的开关：
打开后，高难度子任务会优先派给能力冗余更大的 Agent，进一步平衡负载。

自适应打分函数
--------------
启用 ``scheduling="adaptive"`` 后，使用历史状态驱动的评分：

::

    score = w_cap * capability
          + w_success * success_rate
          + w_difficulty_match * difficulty_match
          + w_trust * trust_score
          - w_load * load_penalty
          - w_error * error_rate
          - w_latency * latency_penalty

其中 ``difficulty_match`` 根据 Agent 能力与任务难度的距离计算，
``error_rate`` 包含执行失败和已识别幻觉，``latency_penalty`` 使用历史平均响应时间归一化。
"""

from __future__ import annotations

import json
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Sequence

from agents.task_agent import DOMAINS, AgentResult, SubTask, TaskAgent
from blockchain.chain_client import NullLedger
from communication import tcp_socket as tc
from config import get_logger, settings
from crypto import hash_tool as ht
from crypto import rsa_tool as rt
from llm.llm_api import TaskKind
from llm.llm_base import BaseLLM

logger = get_logger("blkmac.controller")

__all__ = ["ScoreWeights", "Assignment", "TaskTrace", "ControllerAgent"]

# 冗余调度中「值得花预算」的最小边际收益。相关性≈1（同质 Agent）时
# gain = P(失败)×Rel×(1−ρ) 会退化成纯浮点噪声（~1e-17），必须用阈值挡住，
# 否则会把预算浪费在毫无信息增量的「第三份相同意见」上。
MIN_REDUNDANCY_GAIN = 1e-9


@dataclass
class ScoreWeights:
    """打分函数权重。默认值从 .env 读取。"""

    capability: float = field(default_factory=lambda: settings.SCORE_W_CAPABILITY)
    load: float = field(default_factory=lambda: settings.SCORE_W_LOAD)
    success: float = field(default_factory=lambda: settings.SCORE_W_SUCCESS)
    difficulty: float = field(default_factory=lambda: settings.SCORE_W_DIFFICULTY)
    difficulty_match: float = field(
        default_factory=lambda: settings.SCORE_W_DIFFICULTY_MATCH
    )
    error: float = field(default_factory=lambda: settings.SCORE_W_ERROR)
    latency: float = field(default_factory=lambda: settings.SCORE_W_LATENCY)
    trust: float = field(default_factory=lambda: settings.SCORE_W_TRUST)

    def to_dict(self) -> dict:
        return {
            "w_capability": self.capability,
            "w_load": self.load,
            "w_success": self.success,
            "w_difficulty": self.difficulty,
            "w_difficulty_match": self.difficulty_match,
            "w_error": self.error,
            "w_latency": self.latency,
            "w_trust": self.trust,
        }


@dataclass
class Assignment:
    """一次派单决策，保留完整打分明细供论文分析。"""

    subtask: SubTask
    agent_id: str
    score: float
    all_scores: dict[str, float] = field(default_factory=dict)
    score_breakdown: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "subtask_id": self.subtask.id,
            "domain": self.subtask.domain,
            "difficulty": self.subtask.difficulty,
            "agent_id": self.agent_id,
            "score": round(self.score, 4),
            "all_scores": {k: round(v, 4) for k, v in self.all_scores.items()},
            "score_breakdown": {
                k: round(v, 4) for k, v in self.score_breakdown.items()
            },
        }


@dataclass
class TaskTrace:
    """一道题的完整执行轨迹。"""

    question_id: str
    question: str
    final_answer: str
    subtasks: list[dict] = field(default_factory=list)
    assignments: list[dict] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    chain_receipts: list[dict] = field(default_factory=list)
    # 耗时拆成两部分，口径必须与 A 组一致才能横向比较：
    #   llm_latency_ms       —— 所有 LLM 调用的耗时之和（拆解 + 各子任务 + 汇总）
    #   framework_latency_ms —— 框架自身开销（打分、RSA 加解密、TCP 往返、上链等待）
    #   total_latency_ms     —— 两者之和
    llm_latency_ms: float = 0.0
    framework_latency_ms: float = 0.0
    total_latency_ms: float = 0.0
    hallucination_count: int = 0
    chain_enabled: bool = False
    tamper_detected: int = 0
    # 冗余验证决策轨迹：每条记录说明「给哪个子任务加了谁、边际收益与相关性多少」，
    # REDUNDANCY_BUDGET=0 时恒为空列表（不影响既有口径）。
    redundancy: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "question": self.question,
            "final_answer": self.final_answer,
            "subtasks": self.subtasks,
            "assignments": self.assignments,
            "results": self.results,
            "chain_receipts": self.chain_receipts,
            "llm_latency_ms": round(self.llm_latency_ms, 2),
            "framework_latency_ms": round(self.framework_latency_ms, 2),
            "total_latency_ms": round(self.total_latency_ms, 2),
            "hallucination_count": self.hallucination_count,
            "chain_enabled": self.chain_enabled,
            "tamper_detected": self.tamper_detected,
            "redundancy": self.redundancy,
        }


class ControllerAgent:
    """任务拆解 + 动态分配 + 上链存证的中央控制器。"""

    DECOMPOSE_SYSTEM = (
        "你是一个任务规划器。把用户的复杂问题拆解成若干互相独立的子任务。"
        "只输出 JSON 数组，不要任何解释文字。数组每个元素形如："
        '{"id":"S1","description":"...","domain":"math","difficulty":2}。'
        f"domain 只能取：{'、'.join(DOMAINS)}。difficulty 取 1~5 的整数。"
    )
    AGGREGATE_SYSTEM = (
        "你是一个结果汇总器。根据各子任务的执行结果，给出对原问题的最终答案。"
        "只输出答案本身，不要复述子任务。"
    )

    def __init__(
        self,
        llm: BaseLLM,
        agents: Sequence[TaskAgent],
        ledger: Any = None,
        weights: ScoreWeights | None = None,
        node_id: str = "controller",
        transport: str = "direct",
        scheduling: str = "static",
        trust_aware: bool = False,
        difficulty_matching: bool = True,
        private_key: Any = None,
        redundancy_budget: int = 0,
        correlation_aware: bool = True,
        redundancy_strategy: str = "greedy",
    ) -> None:
        if not agents:
            raise ValueError("至少需要一个 Task Agent")

        self.llm = llm
        self.agents = list(agents)
        self.ledger = ledger if ledger is not None else NullLedger()
        self.weights = weights or ScoreWeights()
        self.node_id = node_id

        if transport not in {"direct", "tcp"}:
            raise ValueError(f"transport 只能是 direct 或 tcp，收到 {transport!r}")
        self.transport = transport
        if scheduling not in {"static", "adaptive"}:
            raise ValueError(
                f"scheduling 只能是 static 或 adaptive，收到 {scheduling!r}"
            )
        self.scheduling = scheduling
        self.trust_aware = trust_aware
        self.difficulty_matching = difficulty_matching
        # 冗余验证预算（额外 LLM 调用次数）。默认 0 = 关闭，行为与既有 A/B/C 一致。
        self.redundancy_budget = max(0, int(redundancy_budget))
        # 是否启用「相关性打折」。False = 朴素信誉加权冗余（实验基线 B2），
        # 与 Proposed 只差这一项，用于隔离相关性建模的贡献。
        self.correlation_aware = bool(correlation_aware)
        # 冗余对象的选择策略：greedy=风险感知贪心（Proposed / B2），
        # random=随机挑子任务与验证者（实验基线 B1）。
        if redundancy_strategy not in {"greedy", "random"}:
            raise ValueError(
                f"redundancy_strategy 只能是 greedy 或 random，收到 {redundancy_strategy!r}"
            )
        self.redundancy_strategy = redundancy_strategy
        # 最近一次 add_redundancy 的决策轨迹（供论文分析与出图）
        self.redundancy_trace: list[dict] = []

        # 与 TaskAgent 同理：direct 模式用不到密钥，惰性生成
        self._private_key = private_key
        self.registry = tc.KeyRegistry()
        self._client: tc.SecureClient | None = None

        self._round_robin_cursor = 0
        self.traces: list[TaskTrace] = []

    # ------------------------------------------------------------------
    # TCP 模式的连线
    # ------------------------------------------------------------------

    @property
    def private_key(self) -> Any:
        """Controller 的 RSA 私钥，首次访问时才生成。"""
        if self._private_key is None:
            self._private_key = rt.generate_keypair()[0]
        return self._private_key

    @property
    def public_key(self) -> Any:
        return self.private_key.publickey()

    def wire_tcp(self) -> None:
        """启动所有 Agent 的服务端并互换公钥。

        直接互相登记公钥（而不是走在线握手），因为在私有联盟场景下
        节点身份本就是预先可信的，省掉一轮网络往返。
        """
        self._client = tc.SecureClient(self.node_id, self.private_key, self.registry)
        for agent in self.agents:
            agent.serve()
            agent.registry.register(self.node_id, self.public_key)
            self.registry.register(agent.agent_id, agent.public_key)

    def shutdown(self) -> None:
        for agent in self.agents:
            agent.stop()

    # ------------------------------------------------------------------
    # 1. 任务拆解
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_json_array(text: str) -> list | None:
        """从 LLM 输出里抠出 JSON 数组。

        真实模型经常在 JSON 外面裹一层 ```json 代码块或前后加解释文字，
        直接 ``json.loads`` 必失败。这是接入真实模型时最高频的故障点，
        因此这里做三级容错。
        """
        text = text.strip()
        if not text:
            return None

        # 一级：直接解析
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            pass

        # 二级：剥掉 markdown 代码块围栏
        fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
        if fenced:
            try:
                parsed = json.loads(fenced.group(1))
                return parsed if isinstance(parsed, list) else None
            except json.JSONDecodeError:
                pass

        # 三级：正则抓第一个方括号块
        bracket = re.search(r"\[.*\]", text, re.DOTALL)
        if bracket:
            try:
                parsed = json.loads(bracket.group(0))
                return parsed if isinstance(parsed, list) else None
            except json.JSONDecodeError:
                pass

        return None

    def decompose(self, question: str, question_id: str = "Q") -> list[SubTask]:
        """把复杂问题拆成子任务。解析失败时降级为「整题作为单个子任务」。"""
        prompt = (
            f"请把下面的问题拆解成子任务：\n{question}\n\n"
            f"只输出 JSON 数组。"
        )
        response = self.llm.generate(prompt, system=self.DECOMPOSE_SYSTEM, kind=TaskKind.DECOMPOSE)
        raw = self._extract_json_array(response.text)

        if not raw:
            logger.warning("任务拆解解析失败，降级为单任务：%s", response.text[:120])
            return [
                SubTask(
                    id=f"{question_id}-S1",
                    description=question,
                    domain="knowledge",
                    difficulty=2,
                    parent_id=question_id,
                )
            ]

        subtasks: list[SubTask] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            subtasks.append(
                SubTask(
                    id=f"{question_id}-{item.get('id') or f'S{i + 1}'}",
                    description=str(item.get("description") or question),
                    domain=str(item.get("domain") or "knowledge"),
                    difficulty=int(item.get("difficulty") or 1),
                    parent_id=question_id,
                )
            )

        if not subtasks:  # 数组里全是垃圾元素
            return self.decompose_fallback(question, question_id)
        return subtasks

    @staticmethod
    def decompose_fallback(question: str, question_id: str = "Q") -> list[SubTask]:
        return [
            SubTask(
                id=f"{question_id}-S1",
                description=question,
                domain="knowledge",
                difficulty=2,
                parent_id=question_id,
            )
        ]

    # ------------------------------------------------------------------
    # 2. 能力打分
    # ------------------------------------------------------------------

    def _score_details(
        self,
        agent: TaskAgent,
        subtask: SubTask,
        max_load: int | None = None,
        max_latency: float | None = None,
    ) -> dict[str, float]:
        """返回一次派单的各评分项，供评分和轨迹记录共同使用。"""
        w = self.weights
        max_load = max_load or max(1, len(self.agents))

        capability_term = agent.capability(subtask.domain)
        normalized_difficulty = (subtask.difficulty - 1) / 4.0  # 1~5 -> 0~1
        difficulty_match_term = (
            1.0 - abs(capability_term - normalized_difficulty)
            if self.difficulty_matching
            else 0.0
        )

        if self.scheduling == "adaptive":
            max_latency = max_latency or max(
                (a.avg_latency_ms for a in self.agents), default=0.0
            )
            latency_term = (
                0.0
                if max_latency <= 0 or agent.task_count == 0
                else min(agent.avg_latency_ms / max_latency, 1.0)
            )
            load_term = min(agent.load, max_load) / max_load
            success_term = agent.success_rate
            error_term = agent.error_rate
            trust_term = agent.trust_score if self.trust_aware else 0.0
            total = (
                w.capability * capability_term
                + w.success * success_term
                + w.difficulty_match * difficulty_match_term
                + w.trust * trust_term
                - w.load * load_term
                - w.error * error_term
                - w.latency * latency_term
            )
            return {
                "capability": capability_term,
                "success_rate": success_term,
                "difficulty_match": difficulty_match_term,
                "load_penalty": load_term,
                "error_rate": error_term,
                "latency_penalty": latency_term,
                "trust_score": trust_term,
                "total": total,
            }

        # 静态基线：当前在办越少，得分越高；难度项保持兼容。
        load_term = 1.0 - min(agent.load, max_load) / max_load
        success_term = agent.success_rate
        difficulty_term = normalized_difficulty * (1.0 - capability_term)
        total = (
            w.capability * capability_term
            + w.load * load_term
            + w.success * success_term
            - w.difficulty * difficulty_term
        )
        return {
            "capability": capability_term,
            "load": load_term,
            "success_rate": success_term,
            "difficulty_penalty": difficulty_term,
            "total": total,
        }

    def score(
        self,
        agent: TaskAgent,
        subtask: SubTask,
        max_load: int | None = None,
        max_latency: float | None = None,
    ) -> float:
        """计算「该 Agent 承接该子任务」的匹配分，越高越合适。"""
        return self._score_details(agent, subtask, max_load, max_latency)["total"]

    # ------------------------------------------------------------------
    # 3. 任务分配
    # ------------------------------------------------------------------

    def assign(self, subtasks: Sequence[SubTask]) -> list[Assignment]:
        """动态打分分配（实验组C 使用）。

        逐个子任务贪心选最高分，并**立即把选中 Agent 的 load 加一**，
        这样下一个子任务的负载项就会自动惩罚它，从而自然形成负载均衡。
        这一步是与对照组B 拉开差距的关键。
        """
        assignments: list[Assignment] = []
        pending: dict[str, int] = {a.agent_id: 0 for a in self.agents}
        max_load = max(1, len(self.agents))
        max_latency = max((a.avg_latency_ms for a in self.agents), default=0.0)

        for subtask in subtasks:
            scores: dict[str, float] = {}
            details: dict[str, dict[str, float]] = {}
            for agent in self.agents:
                # 把本轮已派出但尚未开始执行的任务计入负载
                effective_load = agent.load + pending[agent.agent_id]
                original = agent.load
                agent.load = effective_load
                try:
                    details[agent.agent_id] = self._score_details(
                        agent, subtask, max_load, max_latency
                    )
                    scores[agent.agent_id] = details[agent.agent_id]["total"]
                finally:
                    agent.load = original

            best_id = max(scores, key=scores.get)
            pending[best_id] += 1
            assignments.append(
                Assignment(
                    subtask=subtask,
                    agent_id=best_id,
                    score=scores[best_id],
                    all_scores=scores,
                    score_breakdown=details[best_id],
                )
            )

        return assignments

    # ------------------------------------------------------------------
    # 3.5 风险感知的冗余验证调度（Proposed）
    # ------------------------------------------------------------------

    def add_redundancy(
        self,
        assignments: Sequence[Assignment],
        budget: int,
        correlationAware: bool = True,
        strategy: str | None = None,
    ) -> dict[str, list[str]]:
        """在预算内为子任务贪心追加第二/第三意见。

        边际收益定义::

            gain(subtask, agent j) = P(当前链失败) × Rel_j(domain) × (1 − ρ_j,链)

        三项分别是「当前还有多大出错概率」「新 Agent 本身多可靠」「它是否只是重复
        同样的错误」。``correlationAware=False`` 时把 ρ 置 0，即退化为**朴素信誉加权
        冗余**（实验基线 B2），这样 Proposed 与 B2 只差「相关性打折」一项。
        ``strategy="random"`` 则不看收益，随机挑子任务与验证者（实验基线 B1）。

        :return: ``{subtask_id: [追加的 agent_id, ...]}``，仅在确有追加时出现该键。
        """
        chosenStrategy = strategy or self.redundancy_strategy
        redundancy: dict[str, list[str]] = {}
        trace: list[dict] = []
        if budget <= 0 or not assignments:
            self.redundancy_trace = trace
            return redundancy

        if chosenStrategy == "random":
            plan, trace = self._random_redundancy(assignments, budget)
            self.redundancy_trace = trace
            return plan

        remaining = int(budget)
        while remaining > 0:
            best: tuple[float, str, str, float, float] | None = None
            for assignment in assignments:
                subtask = assignment.subtask
                # 链条**始终**以主 Agent 打头：否则第二轮会忘记主 Agent，
                # 把它当成候选再选一次（重复验证 + 浪费预算）。
                chain = [assignment.agent_id] + redundancy.get(subtask.id, [])
                headAgent = self._agent_by_id(chain[-1])
                failProbability = 1.0 - headAgent.reliability(subtask.domain)
                for candidate in self.agents:
                    if candidate.agent_id in chain:
                        continue
                    if correlationAware:
                        correlation = max(
                            candidate.error_correlation(
                                self._agent_by_id(member), subtask.domain
                            )
                            for member in chain
                        )
                    else:
                        correlation = 0.0
                    reliability = candidate.reliability(subtask.domain)
                    # 相关性只用于**打折**，不用于加权放大：Pearson 可以为负，
                    # 若直接算 (1-ρ) 会得到 >1 的系数，让验证者盖过主 Agent、
                    # 把本来正确的答案翻掉（实测会把独立场景的正确率拉低）。
                    discount = 1.0 - max(0.0, correlation)
                    gain = failProbability * reliability * discount
                    if best is None or gain > best[0]:
                        best = (
                            gain,
                            subtask.id,
                            candidate.agent_id,
                            correlation,
                            reliability,
                        )
            # 阈值而非 >0：相关性≈1 时 gain 只剩浮点噪声，不该拿它去消耗预算。
            if best is None or best[0] <= MIN_REDUNDANCY_GAIN:
                break
            gain, subtaskId, agentId, correlation, reliability = best
            redundancy.setdefault(subtaskId, []).append(agentId)
            trace.append(
                {
                    "subtask_id": subtaskId,
                    "agent_id": agentId,
                    "domain": next(
                        a.subtask.domain for a in assignments if a.subtask.id == subtaskId
                    ),
                    "gain": round(gain, 6),
                    "correlation": round(correlation, 6),
                    "reliability": round(reliability, 6),
                    "rank": len(redundancy[subtaskId]),
                }
            )
            remaining -= 1

        self.redundancy_trace = trace
        return redundancy

    def _random_redundancy(
        self,
        assignments: Sequence[Assignment],
        budget: int,
    ) -> tuple[dict[str, list[str]], list[dict]]:
        """B1 基线：不看风险、不看相关性，随机挑子任务与验证者。

        随机源用全局 ``random``（由 ExperimentRunner 按 seed 播种），因此可复现。
        """
        plan: dict[str, list[str]] = {}
        trace: list[dict] = []
        subtaskIds = [assignment.subtask.id for assignment in assignments]
        primaryBySubtask = {a.subtask.id: a.agent_id for a in assignments}
        domainBySubtask = {a.subtask.id: a.subtask.domain for a in assignments}
        attempts = 0
        maxAttempts = max(10, int(budget) * 20)
        while len(trace) < int(budget) and attempts < maxAttempts:
            attempts += 1
            subtaskId = random.choice(subtaskIds)
            chain = [primaryBySubtask[subtaskId]] + plan.get(subtaskId, [])
            candidates = [a for a in self.agents if a.agent_id not in chain]
            if not candidates:
                continue
            picked = random.choice(candidates)
            plan.setdefault(subtaskId, []).append(picked.agent_id)
            trace.append(
                {
                    "subtask_id": subtaskId,
                    "agent_id": picked.agent_id,
                    "domain": domainBySubtask[subtaskId],
                    "gain": None,
                    "correlation": None,
                    "reliability": round(
                        picked.reliability(domainBySubtask[subtaskId]), 6
                    ),
                    "rank": len(plan[subtaskId]),
                }
            )
        return plan, trace

    def assign_round_robin(self, subtasks: Sequence[SubTask]) -> list[Assignment]:
        """固定顺序轮询分配（对照组B 使用）。

        不看领域、不看负载、不看历史——这正是"普通多智能体"的做法。
        结果是 math 任务经常落到不擅长 math 的 Agent 手里，幻觉率随之上升。
        """
        assignments = []
        for subtask in subtasks:
            agent = self.agents[self._round_robin_cursor % len(self.agents)]
            self._round_robin_cursor += 1
            assignments.append(Assignment(subtask=subtask, agent_id=agent.agent_id, score=0.0))
        return assignments

    def _agent_by_id(self, agent_id: str) -> TaskAgent:
        for agent in self.agents:
            if agent.agent_id == agent_id:
                return agent
        raise KeyError(f"找不到 Agent {agent_id!r}")

    # ------------------------------------------------------------------
    # 4. 派发执行
    # ------------------------------------------------------------------

    def dispatch(self, assignment: Assignment) -> AgentResult:
        """把子任务交给指定 Agent 执行。"""
        agent = self._agent_by_id(assignment.agent_id)

        if self.transport == "direct":
            return agent.execute(assignment.subtask)

        # --- 加密 TCP 模式 ---
        if self._client is None:
            raise RuntimeError("TCP 模式需要先调用 wire_tcp()")
        host, port = agent.address
        reply = self._client.send(
            host, port, agent.agent_id, assignment.subtask.to_payload(), msg_type=tc.MsgType.TASK
        )
        payload = reply.payload
        if not isinstance(payload, dict):
            raise tc.ProtocolError(f"Agent 返回格式非法：{type(payload).__name__}")
        return AgentResult(
            **{k: v for k, v in payload.items() if k in AgentResult.__annotations__}
        )

    # ------------------------------------------------------------------
    # 5. 汇总
    # ------------------------------------------------------------------

    def aggregate(self, question: str, results: Sequence[AgentResult]) -> str:
        """把子结果汇总成最终答案。

        单个子任务时直接返回其答案——多绕一次 LLM 只会平白引入新的出错机会。
        """
        usable = [r for r in results if r.ok and r.answer]
        if not usable:
            return ""
        if len(usable) == 1:
            return usable[0].answer

        summary = "\n".join(f"- 子任务 {r.subtask_id}：{r.answer}" for r in usable)
        prompt = f"原问题：{question}\n\n各子任务结果：\n{summary}\n\n请给出最终答案。"
        return self.llm.generate(
            prompt, system=self.AGGREGATE_SYSTEM, kind=TaskKind.AGGREGATE
        ).clean_text

    def aggregate_redundant(
        self,
        subtask: SubTask,
        results: Sequence[AgentResult],
        correlationAware: bool = True,
    ) -> str:
        """去相关加权投票：高度相关的同答案只算一份打折证据。

        做法：按后验可靠性从高到低逐个纳入，每个 Agent 的权重为
        ``Rel_j(domain) × (1 − max_{已纳入} ρ_jk)``——即「已被一个高度相关的
        同伴支持过」的答案不再获得全额支持。
        ``correlationAware=False`` 时 ρ 恒为 0，退化为**朴素信誉加权投票**
        （实验基线 B2 的聚合策略）。
        """
        agents = {a.agent_id: a for a in self.agents}
        usable = [r for r in results if r.ok and r.answer]
        if not usable:
            return ""

        ordered = sorted(
            usable,
            key=lambda r: -agents[r.agent_id].reliability(subtask.domain)
            if r.agent_id in agents
            else 0.0,
        )
        weights: dict[str, float] = {}
        selected: list[str] = []
        for result in ordered:
            agent = agents.get(result.agent_id)
            if agent is None:
                continue
            reliability = agent.reliability(subtask.domain)
            if correlationAware and selected:
                correlation = max(
                    agent.error_correlation(agents[member], subtask.domain)
                    for member in selected
                )
            else:
                correlation = 0.0
            weights[result.agent_id] = reliability * (1.0 - max(0.0, correlation))
            selected.append(result.agent_id)

        score: dict[str, float] = defaultdict(float)
        for result in usable:
            score[result.answer] += weights.get(result.agent_id, 0.0)

        if not score:
            return usable[0].answer
        totalWeight = sum(score.values())
        if totalWeight <= 0.0:
            # 全部证据都被相关性打折到 0：退回多数投票，再以最高可靠性者的答案破平
            counts: dict[str, int] = defaultdict(int)
            for result in usable:
                counts[result.answer] += 1
            bestCount = max(counts.values())
            tied = [answer for answer, count in counts.items() if count == bestCount]
            if len(tied) == 1:
                return tied[0]
            for result in ordered:
                if result.answer in tied:
                    return result.answer
            return tied[0]
        return max(score, key=score.get)

    def _merge_redundant(
        self,
        subtask: SubTask,
        primary: AgentResult,
        secondary: Sequence[AgentResult],
    ) -> AgentResult:
        """把主 Agent 与冗余验证 Agent 的结果合成一条最终结果。

        规则（保持可解释，便于论文复现）：
        * 主结果可用 → 在「主结果 + 可用的冗余结果」上做去相关加权投票；
        * 主结果失败但冗余结果可用 → 取后验可靠性最高的冗余结果（冗余带来的可用性收益）；
        * 全部不可用 → 原样返回主结果。
        """
        usablePrimary = [primary] if (primary.ok and primary.answer) else []
        usableSecondary = [r for r in secondary if r.ok and r.answer]
        allResults = usablePrimary + usableSecondary

        if not allResults:
            return primary
        if not usablePrimary:
            return max(
                usableSecondary,
                key=lambda r: self._agent_by_id(r.agent_id).reliability(subtask.domain)
                if r.agent_id in {a.agent_id for a in self.agents}
                else 0.0,
            )
        if not usableSecondary:
            return primary

        answer = self.aggregate_redundant(
            subtask, allResults, correlationAware=self.correlation_aware
        )
        return AgentResult(
            subtask_id=subtask.id,
            agent_id=primary.agent_id,
            answer=answer,
            domain=subtask.domain,
            difficulty=subtask.difficulty,
            # 最终答案若来自某个幻觉回答，则整条结果标记为幻觉，指标口径不变
            hallucinated=any(r.hallucinated and r.answer == answer for r in allResults),
            # 成本如实计入：这条结果的耗时是本次冗余验证的总 LLM 耗时
            latency_ms=sum(r.latency_ms for r in allResults),
            llm_model=primary.llm_model,
            digest=ht.message_digest(
                {
                    "subtask_id": subtask.id,
                    "agent_id": primary.agent_id,
                    "answer": answer,
                    "error": "",
                }
            ),
        )

    # ------------------------------------------------------------------
    # 6. 完整流程
    # ------------------------------------------------------------------
    def run(
        self,
        question: str,
        question_id: str = "Q",
        use_scoring: bool = True,
        upload_chain: bool = True,
    ) -> TaskTrace:
        """跑完一道题：拆解 → 分配 → 执行 → 存证 → 汇总。

        :param use_scoring:  True 用动态打分（组C），False 用轮询（组B）
        :param upload_chain: 是否把每条交互记录上链
        """
        start = time.perf_counter()
        # 记录基线，用差值算出本轮 Controller 自身 LLM 调用（拆解 + 汇总）的耗时
        controller_llm_baseline = self.llm.total_latency_ms
        trace = TaskTrace(question_id=question_id, question=question, final_answer="")

        subtasks = self.decompose(question, question_id)
        trace.subtasks = [s.to_dict() for s in subtasks]

        assignments = self.assign(subtasks) if use_scoring else self.assign_round_robin(subtasks)
        trace.assignments = [a.to_dict() for a in assignments]

        # --- 风险感知的冗余验证：在预算内决定「给哪个子任务加第二/第三意见」---
        # REDUNDANCY_BUDGET=0（默认）时该分支完全不触发，行为与既有 A/B/C 一致。
        self.redundancy_trace = []
        redundancy: dict[str, list[str]] = {}
        if self.redundancy_budget > 0:
            redundancy = self.add_redundancy(
                assignments,
                self.redundancy_budget,
                correlationAware=self.correlation_aware,
                strategy=self.redundancy_strategy,
            )
            trace.redundancy = list(self.redundancy_trace)

        results: list[AgentResult] = []
        for assignment in assignments:
            try:
                result = self.dispatch(assignment)
                extraIds = redundancy.get(assignment.subtask.id, [])
                if extraIds:
                    secondary: list[AgentResult] = []
                    for extraId in extraIds:
                        try:
                            secondary.append(
                                self.dispatch(
                                    Assignment(
                                        subtask=assignment.subtask,
                                        agent_id=extraId,
                                        score=0.0,
                                    )
                                )
                            )
                        except Exception as exc:  # noqa: BLE001
                            # 冗余 Agent 失败不应连累主结果，只跳过这一个验证者
                            logger.warning(
                                "冗余验证派发 %s → %s 失败：%s",
                                assignment.subtask.id,
                                extraId,
                                exc,
                            )
                    result = self._merge_redundant(
                        assignment.subtask, result, secondary
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("派发 %s 失败：%s", assignment.subtask.id, exc)
                result = AgentResult(
                    subtask_id=assignment.subtask.id,
                    agent_id=assignment.agent_id,
                    answer="",
                    domain=assignment.subtask.domain,
                    difficulty=assignment.subtask.difficulty,
                    error=str(exc),
                )
            results.append(result)

            # --- 上链存证 ---
            # 【待 Ganache 私有链部署启动后，再进行联调】
            # CHAIN_ENABLED=false 时 ledger 是 NullLedger，这里返回 status=skipped，
            # 不会伪装成已上链。
            if upload_chain:
                try:
                    receipt = self.ledger.store(
                        result.digest or ht.message_digest(result.to_dict()),
                        assignment.subtask.id,
                        self.node_id,
                        assignment.agent_id,
                    )
                    trace.chain_receipts.append(receipt)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("上链失败 %s：%s", assignment.subtask.id, exc)
                    trace.chain_receipts.append({"status": "failed", "error": str(exc)})

        trace.results = [r.to_dict() for r in results]
        trace.hallucination_count = sum(1 for r in results if r.hallucinated)
        trace.final_answer = self.aggregate(question, results)

        # LLM 耗时 = Controller 的拆解/汇总调用 + 各 Agent 执行子任务的调用
        trace.llm_latency_ms = (self.llm.total_latency_ms - controller_llm_baseline) + sum(
            r.latency_ms for r in results
        )
        # 框架开销 = 墙钟总耗时 - LLM 调用耗时，避免真实后端网络等待被重复计数。
        wall_elapsed_ms = (time.perf_counter() - start) * 1000
        if getattr(self.llm, "provider", "") == "mock":
            trace.framework_latency_ms = wall_elapsed_ms
        else:
            trace.framework_latency_ms = max(0.0, wall_elapsed_ms - trace.llm_latency_ms)
        trace.total_latency_ms = trace.llm_latency_ms + trace.framework_latency_ms
        trace.chain_enabled = bool(getattr(self.ledger, "enabled", False))
        trace.tamper_detected = sum(len(a.tamper_events) for a in self.agents)

        self.traces.append(trace)
        return trace

    # ------------------------------------------------------------------

    def reset(self) -> None:
        """重置控制器与所有 Agent 的统计量，供切换实验组时复用。"""
        self._round_robin_cursor = 0
        self.traces.clear()
        self.llm.reset_stats()
        for agent in self.agents:
            agent.reset()

    def agent_stats(self) -> list[dict]:
        return [a.stats() for a in self.agents]

    def __repr__(self) -> str:
        return (
            f"<ControllerAgent agents={len(self.agents)} transport={self.transport} "
            f"ledger={type(self.ledger).__name__}>"
        )
