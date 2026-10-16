"""Task Agent —— 执行子任务的工作智能体。

每个 Agent 具备：

* **领域能力向量** ``capabilities``：``{"math": 0.9, "code": 0.4, ...}``，
  取值 0~1，表示在该领域的胜任程度。这是 Controller 打分派单的主要依据。
* **独立 RSA 密钥对**：用于加密 TCP 通信中的签名与解密。
* **负载与历史统计**：当前在办任务数、历史成功率，参与打分。

支持两种调用方式：

``direct``
    Controller 直接在进程内调用 :meth:`TaskAgent.execute`。快，用于跑大批量实验。
``tcp``
    Controller 通过加密 TCP 信道下发任务（:meth:`TaskAgent.serve`）。
    这是论文要求的完整安全链路，但每条消息都要做 RSA 运算，慢得多。

两种方式产出的 :class:`AgentResult` 完全一致，因此对照实验可以自由切换。
"""

from __future__ import annotations

import statistics
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from communication import tcp_socket as tc
from config import get_logger, settings
from crypto import hash_tool as ht
from crypto import rsa_tool as rt
from llm.llm_api import TaskKind
from llm.llm_base import BaseLLM

logger = get_logger("blkmac.agent")

__all__ = [
    "SubTask",
    "AgentResult",
    "TaskAgent",
    "DOMAINS",
    "make_agent_pool",
    "default_capability_profiles",
]

# 题库与能力向量共用的领域集合
DOMAINS = ("math", "code", "logic", "knowledge", "writing")


@dataclass
class SubTask:
    """一个待执行的子任务。"""

    id: str
    description: str
    domain: str = "knowledge"
    difficulty: int = 1
    parent_id: str = ""
    expected: str | None = None  # 标准答案，仅用于实验评分，不传给 Agent 的提示词

    def __post_init__(self):
        if self.domain not in DOMAINS:
            self.domain = "knowledge"
        self.difficulty = max(1, min(5, int(self.difficulty)))

    def to_dict(self) -> dict:
        return asdict(self)

    def to_payload(self) -> dict:
        """下发给 Agent 的内容——**不含标准答案**，防止实验作弊。"""
        return {
            "id": self.id,
            "description": self.description,
            "domain": self.domain,
            "difficulty": self.difficulty,
            "parent_id": self.parent_id,
        }


@dataclass
class AgentResult:
    """子任务的执行结果。"""

    subtask_id: str
    agent_id: str
    answer: str
    domain: str = "knowledge"
    difficulty: int = 1
    hallucinated: bool = False
    latency_ms: float = 0.0
    llm_model: str = ""
    error: str = ""
    digest: str = ""
    timestamp: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict:
        return asdict(self)

    def ledger_record(self) -> dict:
        """上链存证用的精简记录：只留摘要与元数据，不含答案明文。"""
        return {
            "subtask_id": self.subtask_id,
            "agent_id": self.agent_id,
            "domain": self.domain,
            "digest": self.digest,
            "timestamp": self.timestamp,
        }


class TaskAgent:
    """执行子任务的工作智能体。"""

    SYSTEM_PROMPT = (
        "你是一个专注的任务执行智能体。请直接给出答案，不要复述题目，不要解释推理过程。"
    )

    def __init__(
        self,
        agent_id: str,
        llm: BaseLLM,
        capabilities: dict[str, float] | None = None,
        private_key: Any = None,
        host: str | None = None,
        port: int = 0,
        error_group: str | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.llm = llm
        # 未指定的领域按 0.5 中性能力补齐，避免打分时 KeyError
        self.capabilities = {d: 0.5 for d in DOMAINS}
        if capabilities:
            self.capabilities.update({k: float(v) for k, v in capabilities.items()})

        # 错误潜因子分组：同一组的 Agent 在 MockLLM 层共享「是否幻觉/错成什么」，
        # 用于构造「相关错误」这一实验变量。None（默认）= 不启用，行为与既有一致。
        self.error_group = error_group

        # 密钥惰性生成：2048 位 RSA 生成要几百毫秒，而 direct 模式（进程内直接
        # 调用）根本用不到密钥。只有真正走加密 TCP 时才付这个代价。
        self._private_key = private_key

        self.host = host or settings.TCP_HOST
        self._port = port
        self._server: tc.SecureServer | None = None
        self.registry = tc.KeyRegistry()

        # 统计量：load 参与打分，其余供实验出图
        self._lock = threading.RLock()
        self.load = 0
        self.completed = 0
        self.failed = 0
        self.hallucinations = 0
        self.total_latency_ms = 0.0
        self.trust_score = min(1.0, max(0.0, settings.TRUST_INITIAL_SCORE))
        self.trust_updates = 0
        self.trust_last_event = "initial"
        self.trust_history: list[dict[str, Any]] = []
        self.results: list[AgentResult] = []

        # per-domain Beta 可靠性后验（先验 α=β=1 → 均匀），以及逐领域的
        # 「是否出错」指示序列（0=对，1=错/幻觉），供错误相关性估计使用。
        self.alpha: dict[str, float] = {d: settings.BETA_PRIOR_ALPHA for d in DOMAINS}
        self.beta: dict[str, float] = {d: settings.BETA_PRIOR_BETA for d in DOMAINS}
        self.error_flags: dict[str, list[int]] = {d: [] for d in DOMAINS}

    # -- 属性 -------------------------------------------------------------

    @property
    def private_key(self) -> Any:
        """本 Agent 的 RSA 私钥，首次访问时才生成。"""
        if self._private_key is None:
            self._private_key = rt.generate_keypair()[0]
        return self._private_key

    @property
    def public_key(self) -> Any:
        return self.private_key.publickey()

    def capability(self, domain: str) -> float:
        return self.capabilities.get(domain, 0.5)

    @property
    def success_rate(self) -> float:
        """历史成功率。没有历史时给 1.0，避免新 Agent 因无历史而永远抢不到任务。"""
        total = self.completed + self.failed
        return 1.0 if total == 0 else self.completed / total

    @property
    def error_rate(self) -> float:
        """历史质量错误率，包含执行失败和已识别的幻觉。"""
        total = self.task_count
        if total == 0:
            return 0.0
        return min(1.0, (self.failed + self.hallucinations) / total)

    def update_trust(self, result: AgentResult) -> None:
        """根据最近一次结果更新信誉，并保留可审计的更新轨迹。"""
        if not result.ok:
            event = "failure"
            delta = -settings.TRUST_FAILURE_PENALTY
        elif result.hallucinated:
            event = "hallucination"
            delta = -settings.TRUST_HALLUCINATION_PENALTY
        else:
            event = "success"
            delta = settings.TRUST_SUCCESS_REWARD
        previous = self.trust_score
        self.trust_score = min(1.0, max(0.0, self.trust_score + delta))
        self.trust_updates += 1
        self.trust_last_event = event
        self.trust_history.append(
            {
                "subtask_id": result.subtask_id,
                "event": event,
                "previous": round(previous, 6),
                "delta": round(delta, 6),
                "score": round(self.trust_score, 6),
            }
        )

    # -- per-domain 可靠性（Beta 后验）与错误相关性 -------------------------

    def _ensure_domain(self, domain: str) -> str:
        """保证该领域有后验与错误指示序列；未知领域按 knowledge 之外的键单独建档。"""
        if domain not in self.alpha:
            self.alpha[domain] = settings.BETA_PRIOR_ALPHA
            self.beta[domain] = settings.BETA_PRIOR_BETA
            self.error_flags[domain] = []
        return domain

    def update_reliability(self, result: AgentResult) -> None:
        """按领域更新 Beta 后验；错误/幻觉记 beta，成功记 alpha。

        与标量 ``trust_score`` 的区别：标量信任把所有领域混在一起，无法表达
        「该 Agent 在 math 可靠、在 code 不可靠」这类领域差异。
        """
        domain = self._ensure_domain(result.domain)
        if (not result.ok) or result.hallucinated:
            self.beta[domain] += 1.0
        else:
            self.alpha[domain] += 1.0
        self.error_flags[domain].append(
            0 if (result.ok and not result.hallucinated) else 1
        )

    def reliability(self, domain: str) -> float:
        """该领域的后验成功率 = α/(α+β)。无观测时退回均匀先验。"""
        alpha = self.alpha.get(domain, settings.BETA_PRIOR_ALPHA)
        beta = self.beta.get(domain, settings.BETA_PRIOR_BETA)
        total = alpha + beta
        if total <= 0:
            return 0.5
        return alpha / total

    def error_correlation(self, other: "TaskAgent", domain: str) -> float:
        """per-domain 的错误相关性估计。

        冷启动（观测样本不足）：用**能力向量余弦相似度**作先验——两个 math 都弱的
        Agent 能力向量相近 → 先验相关性高，无需额外数据即可表达「同质能力会同错」。
        热启动：对两边最近的错误指示向量求 Pearson 相关系数。
        """
        a = self.error_flags.get(domain, [])
        b = other.error_flags.get(domain, [])
        sampleCount = min(len(a), len(b))
        minSamples = max(1, int(settings.CORRELATION_MIN_SAMPLES))

        if sampleCount < minSamples:
            dot = sum(self.capabilities[d] * other.capabilities[d] for d in DOMAINS)
            normA = sum(v * v for v in self.capabilities.values()) ** 0.5
            normB = sum(v * v for v in other.capabilities.values()) ** 0.5
            if normA <= 0.0 or normB <= 0.0:
                return 0.0
            return max(0.0, min(1.0, dot / (normA * normB)))

        x, y = a[-sampleCount:], b[-sampleCount:]
        # 任一侧错误指示恒定（全错或全对）时 Pearson 无定义，视为「无相关证据」
        if len(set(x)) < 2 or len(set(y)) < 2:
            return 0.0
        return max(-1.0, min(1.0, statistics.correlation(x, y)))

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.completed if self.completed else 0.0
    @property
    def task_count(self) -> int:
        """累计承接的任务数——阶段3「各 agent 任务量」指标。"""
        return self.completed + self.failed

    @property
    def fingerprint(self) -> str:
        return rt.fingerprint(self.public_key)

    # -- 执行 -------------------------------------------------------------

    def _build_prompt(self, subtask: SubTask) -> str:
        return (
            f"【领域】{subtask.domain}\n"
            f"【难度】{subtask.difficulty}/5\n"
            f"【任务】{subtask.description}\n\n"
            f"请直接给出答案。"
        )

    def execute(self, subtask: SubTask | dict) -> AgentResult:
        """执行一个子任务。异常一律转成带 error 的结果，不向上抛。

        原因：一个 Agent 挂掉不应该让整轮实验中断，
        失败本身也是要统计进对照数据的指标。
        """
        if isinstance(subtask, dict):
            subtask = SubTask(**{k: v for k, v in subtask.items() if k in SubTask.__annotations__})

        with self._lock:
            self.load += 1

        start = time.perf_counter()
        try:
            response = self.llm.generate(
                self._build_prompt(subtask),
                system=self.SYSTEM_PROMPT,
                kind=TaskKind.ANSWER,
                # 把本 Agent 在该领域的能力传给 LLM 层：
                # MockLLM 据此决定幻觉概率，真实后端会忽略该参数
                competence=self.capability(subtask.domain),
                difficulty=subtask.difficulty,
                # 错误潜因子分组：同组 Agent 在 MockLLM 层共享幻觉判定，
                # 从而产生「相关错误」。None 时 MockLLM 行为与既有一致。
                error_group=self.error_group,
            )
            result = AgentResult(
                subtask_id=subtask.id,
                agent_id=self.agent_id,
                answer=response.clean_text,
                domain=subtask.domain,
                difficulty=subtask.difficulty,
                hallucinated=response.hallucinated,
                latency_ms=response.latency_ms,
                llm_model=response.model,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Agent %s 执行 %s 失败：%s", self.agent_id, subtask.id, exc)
            result = AgentResult(
                subtask_id=subtask.id,
                agent_id=self.agent_id,
                answer="",
                domain=subtask.domain,
                difficulty=subtask.difficulty,
                latency_ms=(time.perf_counter() - start) * 1000,
                error=str(exc),
            )

        # 摘要覆盖结果全部关键字段，任何一处被改都会导致链上校验失败
        result.digest = ht.message_digest(
            {
                "subtask_id": result.subtask_id,
                "agent_id": result.agent_id,
                "answer": result.answer,
                "error": result.error,
            }
        )

        with self._lock:
            self.load -= 1
            if result.ok:
                self.completed += 1
                self.total_latency_ms += result.latency_ms
            else:
                self.failed += 1
            if result.hallucinated:
                self.hallucinations += 1
            self.update_trust(result)
            self.update_reliability(result)
            self.results.append(result)

        return result

    # -- TCP 服务模式 ------------------------------------------------------

    def _handle_message(self, message: tc.SecureMessage) -> dict:
        """加密 TCP 信道上的任务处理回调。"""
        payload = message.payload
        if not isinstance(payload, dict):
            return {"error": f"任务载荷格式非法：{type(payload).__name__}"}
        return self.execute(SubTask(**{
            k: v for k, v in payload.items() if k in SubTask.__annotations__
        })).to_dict()

    def serve(self) -> tc.SecureServer:
        """启动加密 TCP 服务端。幂等。"""
        if self._server is None:
            self._server = tc.SecureServer(
                node_id=self.agent_id,
                private_key=self.private_key,
                registry=self.registry,
                handler=self._handle_message,
                host=self.host,
                port=self._port,
            )
        self._server.start()
        return self._server

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop()

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            return (self.host, self._port)
        return self._server.address

    @property
    def tamper_events(self) -> list[dict]:
        """本 Agent 检测到的篡改事件。"""
        return self._server.tamper_events if self._server else []

    # -- 统计 -------------------------------------------------------------

    def stats(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "task_count": self.task_count,
            "completed": self.completed,
            "failed": self.failed,
            "hallucinations": self.hallucinations,
            "success_rate": round(self.success_rate, 4),
            "error_rate": round(self.error_rate, 4),
            "trust_score": round(self.trust_score, 4),
            "trust_updates": self.trust_updates,
            "trust_last_event": self.trust_last_event,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "capabilities": dict(self.capabilities),
            "reliability": {d: round(self.reliability(d), 4) for d in DOMAINS},
            "error_group": self.error_group,
        }

    def reset(self) -> None:
        """清空统计，用于在三组对照实验之间复用同一批 Agent 配置。"""
        with self._lock:
            self.load = 0
            self.completed = 0
            self.failed = 0
            self.hallucinations = 0
            self.total_latency_ms = 0.0
            self.trust_score = min(1.0, max(0.0, settings.TRUST_INITIAL_SCORE))
            self.trust_updates = 0
            self.trust_last_event = "initial"
            self.trust_history.clear()
            self.results.clear()
            self.alpha = {d: settings.BETA_PRIOR_ALPHA for d in DOMAINS}
            self.beta = {d: settings.BETA_PRIOR_BETA for d in DOMAINS}
            self.error_flags = {d: [] for d in DOMAINS}
        self.llm.reset_stats()

    def __repr__(self) -> str:
        top = max(self.capabilities, key=self.capabilities.get)
        return f"<TaskAgent {self.agent_id} 专长={top}({self.capabilities[top]:.2f}) 已办={self.task_count}>"


# ---------------------------------------------------------------------------
# Agent 池构造
# ---------------------------------------------------------------------------

# 专家型能力配置：每个 Agent 在 1~2 个领域强、其余弱。
# 这种"能力不均衡"是对照实验成立的前提——如果所有 Agent 各领域能力都一样，
# 动态打分派单与轮询派单的效果不会有任何差别，实验组C 也就无从体现优势。
_CAPABILITY_PROFILES: list[dict[str, float]] = [
    {"math": 0.95, "logic": 0.75, "code": 0.35, "knowledge": 0.40, "writing": 0.25},
    {"code": 0.95, "math": 0.60, "logic": 0.55, "knowledge": 0.35, "writing": 0.30},
    {"knowledge": 0.95, "writing": 0.70, "logic": 0.45, "math": 0.25, "code": 0.20},
    {"writing": 0.95, "knowledge": 0.65, "logic": 0.40, "math": 0.20, "code": 0.25},
    {"logic": 0.95, "math": 0.70, "code": 0.50, "knowledge": 0.45, "writing": 0.35},
]


def default_capability_profiles() -> list[dict[str, float]]:
    """预设专家画像的副本。

    实验构造「相关性档位」时按画像复用 Agent（同画像 → 同错误组 → 相关错误），
    因此需要一份不会被外部改动的副本。
    """
    return [dict(profile) for profile in _CAPABILITY_PROFILES]


def make_agent_pool(
    count: int,
    llm_factory,
    id_prefix: str = "agent",
    host: str | None = None,
    profiles: list[dict[str, float]] | None = None,
    error_groups: list[str | None] | None = None,
) -> list[TaskAgent]:
    """按预设的专家画像批量创建 Agent。

    :param llm_factory: ``callable(agent_index) -> BaseLLM``，
        每个 Agent 拿独立的 LLM 实例，统计互不干扰。
    :param profiles: 可选的能力画像列表（第 i 个 Agent 用 ``profiles[i]``）。
        默认按 ``_CAPABILITY_PROFILES`` 轮转，保持与既有一致。
    :param error_groups: 可选的错误潜因子分组标签列表；``None``（默认）表示
        不启用分组，此时 MockLLM 行为与既有完全一致。
    """
    agents = []
    for i in range(count):
        if profiles is not None:
            profile = dict(profiles[i % len(profiles)])
        else:
            profile = dict(_CAPABILITY_PROFILES[i % len(_CAPABILITY_PROFILES)])
        group = None if error_groups is None else error_groups[i % len(error_groups)]
        agents.append(
            TaskAgent(
                agent_id=f"{id_prefix}-{i + 1}",
                llm=llm_factory(i),
                capabilities=profile,
                host=host,
                error_group=group,
            )
        )
    return agents
