#!/usr/bin/env python3
"""BlkMaC 三组对照实验主调度入口（阶段3）。

三个实验组
----------
========  ==========  ==========  ==========  ==========  ==========
 组        任务拆解    分配策略    加密通信    上链存证    对应论文
========  ==========  ==========  ==========  ==========  ==========
 A         ✗           ✗（单体）    ✗           ✗          基线：单 LLM
 B         ✓           轮询固定     ✗           ✗          普通多智能体
 C         ✓           能力打分     ✓ RSA/TCP   ✓          完整 BlkMaC
========  ==========  ==========  ==========  ==========  ==========

三组跑**同一套题库**、**同一个随机种子**、**同一批 Agent 能力画像**，
唯一的差别就是上表中的策略，因此指标差异只可能来自架构本身。

用法::

    python -m controller.main_controller --mode all
    python -m controller.main_controller --mode c --no-chain
    python -m controller.main_controller --mode all --agents 6 --repeat 3

产出（默认写到 results/）::

    metrics.csv                  三组汇总指标
    per_question.csv             逐题明细
    agent_load.csv               各 Agent 任务量
    trace.jsonl                  完整执行轨迹，供论文附录引用
    fig_accuracy_hallucination.png
    fig_agent_load.png
    fig_latency.png

.. note::
   **【待 Ganache 私有链部署启动后，再进行联调】**
   本机无 Node.js，Ganache 未启动。默认 ``CHAIN_ENABLED=false``，
   C 组走 :class:`NullLedger`，所有 chain_* 指标为 0 且在报告中标注
   ``chain_enabled=False``——不会把「没上链」伪装成「已上链」。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

# 允许直接 `python controller/main_controller.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.controller_agent import ControllerAgent, ScoreWeights  # noqa: E402
from agents.task_agent import (  # noqa: E402
    DOMAINS,
    SubTask,
    TaskAgent,
    default_capability_profiles,
    make_agent_pool,
)
from blockchain.chain_client import ChainError, NullLedger, get_ledger  # noqa: E402
from config import BASE_DIR, get_logger, settings  # noqa: E402
from llm.llm_api import MockLLM, get_llm  # noqa: E402
from llm.llm_base import BaseLLM  # noqa: E402

logger = get_logger("blkmac.experiment")

GROUP_LABELS = {
    "a": "A 单LLM直答",
    "b": "B 普通多智能体",
    "c": "C 完整BlkMaC",
}


@dataclass(frozen=True)
class AblationVariant:
    key: str
    label: str
    scheduling: str
    trust_aware: bool
    difficulty_matching: bool
    use_chain: bool


def build_ablation_variants(use_chain: bool) -> list[AblationVariant]:
    """构造 2.1 消融实验的固定变体。"""
    return [
        AblationVariant("full", "完整方法", "adaptive", True, True, use_chain),
        AblationVariant("no_trust", "去掉 Trust", "adaptive", False, True, use_chain),
        AblationVariant("no_adaptive", "去掉 Adaptive Scheduling", "static", False, True, use_chain),
        AblationVariant("no_blockchain", "去掉 Blockchain", "adaptive", True, True, False),
        AblationVariant("no_difficulty", "去掉 Difficulty Matching", "adaptive", True, False, use_chain),
    ]


# ===========================================================================
# 题库
# ===========================================================================


@dataclass
class Question:
    id: str
    question: str
    answer: str
    domain: str = "knowledge"
    difficulty: int = 1
    parts: list[dict] = field(default_factory=list)

    @property
    def expected_tokens(self) -> list[str]:
        """判定正确所需包含的全部答案片段。"""
        if self.parts:
            return [str(p["answer"]) for p in self.parts]
        return [self.answer]

    def to_subtasks(self) -> list[SubTask]:
        """把 parts 转成 SubTask（B/C 组拆解失败时的兜底）。"""
        if not self.parts:
            return [
                SubTask(f"{self.id}-S1", self.question, self.domain, self.difficulty, self.id)
            ]
        return [
            SubTask(
                id=f"{self.id}-S{i + 1}",
                description=p["description"],
                domain=p.get("domain", self.domain),
                difficulty=int(p.get("difficulty", 1)),
                parent_id=self.id,
                expected=str(p["answer"]),
            )
            for i, p in enumerate(self.parts)
        ]


def load_dataset(path: str | Path | None = None) -> list[Question]:
    """读取题库。"""
    p = Path(path) if path else settings.DATASET_PATH
    if not p.is_absolute():
        p = BASE_DIR / p
    if not p.exists():
        raise SystemExit(f"[错误] 题库不存在：{p}")

    data = json.loads(p.read_text(encoding="utf-8"))
    raw = data["questions"] if isinstance(data, dict) else data

    questions = [
        Question(
            id=item["id"],
            question=item["question"],
            answer=str(item["answer"]),
            domain=item.get("domain", "knowledge"),
            difficulty=int(item.get("difficulty", 1)),
            parts=item.get("parts", []),
        )
        for item in raw
    ]
    logger.info("载入题库 %s，共 %d 题", p.name, len(questions))
    return questions


def build_knowledge_base(questions: Sequence[Question]) -> dict[str, str]:
    """构造 MockLLM 的知识库：整题与每个子任务都要能查到答案。"""
    kb: dict[str, str] = {}
    for q in questions:
        kb[q.question] = q.answer
        for p in q.parts:
            kb[p["description"]] = str(p["answer"])
    return kb


def build_decomposition_plan(questions: Sequence[Question]) -> dict[str, list[dict]]:
    """构造 MockLLM 的拆解计划：原题 -> 子任务列表。"""
    return {
        q.question: [
            {
                "id": f"S{i + 1}",
                "description": p["description"],
                "domain": p.get("domain", q.domain),
                "difficulty": int(p.get("difficulty", 1)),
            }
            for i, p in enumerate(q.parts)
        ]
        for q in questions
        if q.parts
    }


# ===========================================================================
# 评分
# ===========================================================================

_NORMALIZE_RE = re.compile(r"[\s，,。．\.；;：:！!？?、\-—_“”\"'（）()\[\]【】]+")


def _normalize(text: str) -> str:
    """归一化答案文本，消除标点与空白差异带来的假阴性。"""
    return _NORMALIZE_RE.sub("", str(text)).lower()


def is_correct(final_answer: str, expected_tokens: Sequence[str]) -> bool:
    """判定正确：最终答案必须包含全部标准答案片段。

    复合题只答对一半不算对——这正是要考察的「拆解 + 分派」能力。
    """
    if not final_answer:
        return False
    normalized = _normalize(final_answer)
    return all(_normalize(t) in normalized for t in expected_tokens if str(t).strip())


# ===========================================================================
# 实验结果容器
# ===========================================================================


@dataclass
class QuestionRecord:
    group: str
    question_id: str
    domain: str
    difficulty: int
    correct: bool
    hallucinations: int
    latency_ms: float
    llm_latency_ms: float
    framework_latency_ms: float
    subtask_count: int
    agent_ids: str
    chain_tx: int
    chain_gas: int
    final_answer: str
    # 冗余验证带来的额外 LLM 调用次数（成本口径；无冗余时恒为 0）
    redundant_calls: int = 0


@dataclass
class GroupResult:
    group: str
    label: str
    records: list[QuestionRecord] = field(default_factory=list)
    agent_stats: list[dict] = field(default_factory=list)
    traces: list[dict] = field(default_factory=list)
    chain_enabled: bool = False
    wall_clock_s: float = 0.0
    provider: str = "mock"
    model: str = ""
    scheduling: str = "static"
    trust_aware: bool = False
    difficulty_matching: bool = True
    # 冗余验证配置（无冗余时 budget=0，行为与既有 A/B/C 一致）
    redundancy_budget: int = 0
    correlation_aware: bool = True
    correlation_level: str = "none"
    method: str = "baseline"

    # -- 汇总指标 ---------------------------------------------------------

    @property
    def total(self) -> int:
        return len(self.records)

    @property
    def accuracy(self) -> float:
        return sum(r.correct for r in self.records) / self.total if self.total else 0.0

    @property
    def hallucinations(self) -> int:
        return sum(r.hallucinations for r in self.records)

    @property
    def avg_latency_ms(self) -> float:
        return statistics.fmean(r.latency_ms for r in self.records) if self.records else 0.0

    @property
    def avg_llm_latency_ms(self) -> float:
        return statistics.fmean(r.llm_latency_ms for r in self.records) if self.records else 0.0

    @property
    def avg_framework_latency_ms(self) -> float:
        return (
            statistics.fmean(r.framework_latency_ms for r in self.records)
            if self.records
            else 0.0
        )

    @property
    def agent_loads(self) -> dict[str, int]:
        return {s["agent_id"]: s["task_count"] for s in self.agent_stats}

    @property
    def load_variance(self) -> float:
        """任务量方差 —— 负载均衡程度的量化指标，越小越均衡。"""
        loads = list(self.agent_loads.values())
        return statistics.pvariance(loads) if len(loads) > 1 else 0.0

    @property
    def chain_tx_total(self) -> int:
        return sum(r.chain_tx for r in self.records)

    @property
    def chain_gas_total(self) -> int:
        return sum(r.chain_gas for r in self.records)

    @property
    def redundant_calls_total(self) -> int:
        """冗余验证实际花掉的额外 LLM 调用次数（成本指标）。"""
        return sum(r.redundant_calls for r in self.records)

    @property
    def error_acceptance_rate(self) -> float:
        """错误接受率：最终聚合答案仍然错误的比例（本方案的核心指标）。"""
        return 1.0 - self.accuracy

    def summary(self) -> dict:
        return {
            "group": self.group,
            "label": self.label,
            "questions": self.total,
            "correct": sum(r.correct for r in self.records),
            "accuracy": round(self.accuracy, 4),
            "error_acceptance_rate": round(self.error_acceptance_rate, 4),
            "hallucinations": self.hallucinations,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "avg_llm_latency_ms": round(self.avg_llm_latency_ms, 2),
            "avg_framework_latency_ms": round(self.avg_framework_latency_ms, 2),
            "total_subtasks": sum(r.subtask_count for r in self.records),
            "load_variance": round(self.load_variance, 4),
            "chain_enabled": self.chain_enabled,
            "chain_tx": self.chain_tx_total,
            "chain_gas": self.chain_gas_total,
            "wall_clock_s": round(self.wall_clock_s, 2),
            "provider": self.provider,
            "model": self.model,
            "scheduling": self.scheduling,
            "trust_aware": self.trust_aware,
            "difficulty_matching": self.difficulty_matching,
            "method": self.method,
            "redundancy_budget": self.redundancy_budget,
            "correlation_aware": self.correlation_aware,
            "correlation_level": self.correlation_level,
            "redundant_calls": self.redundant_calls_total,
        }


# ===========================================================================
# 实验运行器
# ===========================================================================


class ExperimentRunner:
    """三组对照实验的统一执行器。"""

    def __init__(
        self,
        questions: Sequence[Question],
        agent_count: int | None = None,
        seed: int | None = None,
        use_chain: bool | None = None,
        transport: str = "direct",
        scheduling: str = "static",
        provider: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        trust_aware: bool = False,
        difficulty_matching: bool = True,
        redundancy_budget: int = 0,
        correlation_aware: bool = True,
        correlation_level: str = "none",
        method: str = "baseline",
    ) -> None:
        self.questions = list(questions)
        self.agent_count = agent_count or settings.AGENT_COUNT
        self.seed = seed if seed is not None else settings.EXPERIMENT_SEED
        self.transport = transport
        if scheduling not in {"static", "adaptive"}:
            raise ValueError(f"scheduling 只能是 static 或 adaptive，收到 {scheduling!r}")
        if trust_aware and scheduling != "adaptive":
            raise ValueError("Trust-aware 调度必须与 scheduling=adaptive 一起启用")
        self.scheduling = scheduling
        self.provider = (provider or settings.LLM_PROVIDER).lower()
        self.model = model or settings.LLM_MODEL
        self.base_url = base_url or settings.LLM_BASE_URL
        self.trust_aware = trust_aware
        self.difficulty_matching = difficulty_matching
        # --- 相关错误感知的可靠调度与验证（PROPOSAL_CORRELATION.md）---
        # 默认全部关闭/中性：redundancy_budget=0 且 correlation_level="none" 时
        # 运行时行为与既有 A/B/C 完全一致。
        self.method = method
        budget = max(0, int(redundancy_budget))
        if method == "baseline":
            # B0：无冗余。强制预算为 0，保证与既有 C 组口径一致。
            budget = 0
            correlation_aware = True
            redundancy_strategy = "greedy"
        elif method == "random":
            # B1：随机加验证 + 多数投票（权重全相同 → 等价多数投票）
            correlation_aware = False
            redundancy_strategy = "random"
        elif method == "reputation":
            # B2：风险最高的子任务优先，但**忽略错误相关性**（强基线）
            correlation_aware = False
            redundancy_strategy = "greedy"
        else:
            # Proposed：相关性感知的边际收益贪心 + 去相关加权投票
            correlation_aware = True
            redundancy_strategy = "greedy"
        self.redundancy_budget = budget
        self.correlation_aware = bool(correlation_aware)
        self.redundancy_strategy = redundancy_strategy
        self.correlation_level = str(correlation_level).lower()

        self.use_chain = settings.CHAIN_ENABLED if use_chain is None else use_chain

        self.knowledge_base = build_knowledge_base(self.questions)
        self.decomposition_plan = build_decomposition_plan(self.questions)

        random.seed(self.seed)

    # -- LLM 构造 ---------------------------------------------------------

    def _make_llm(self, competence: float, salt: int = 0) -> BaseLLM:
        """构造一个 LLM 实例。

        mock 后端会拿到知识库、拆解计划与该执行者的能力值；
        真实后端忽略这些参数，直接走 API。
        """
        if self.provider != "mock":
            kwargs = {"model": self.model}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            return get_llm(self.provider, **kwargs)
        return MockLLM(
            model=self.model,
            seed=self.seed + salt,
            knowledge_base=self.knowledge_base,
            decomposition_plan=self.decomposition_plan,
            competence=competence,
            hallucination_rate=settings.MOCK_HALLUCINATION_RATE,
        )

    def resolve_error_groups(self) -> list[str | None] | None:
        """把相关性档位展开成逐 Agent 的错误分组标签。

        分组与能力画像**对齐**（同画像 → 同组），这样冷启动的相关性先验
        （能力向量余弦）才真正预测得到实际的错误相关性——这是方案 D5 的关键设计。

        * ``none``        —— 不分组（默认）：行为与既有完全一致；
        * ``independent`` —— 4 个互不相同画像，各自一组 → 错误彼此独立；
        * ``pairs``       —— 2 个画像各用 2 次 → 组内相关、组间独立（中等相关）；
        * ``all``         —— 4 个 Agent 同画像同组 → 高度相关。
        """
        mode = self.correlation_level
        count = self.agent_count
        if mode in {"none", "off", ""}:
            return None
        if count <= 1:
            return None
        if mode == "independent":
            return [f"g{i}" for i in range(count)]
        if mode == "pairs":
            return [f"g{i // 2}" for i in range(count)]
        if mode == "all":
            return ["g0"] * count
        raise ValueError(
            f"correlation_level 只能是 none/independent/pairs/all，收到 {self.correlation_level!r}"
        )

    def resolve_profiles(self) -> list[dict[str, float]] | None:
        """把相关性档位展开成逐 Agent 的能力画像（与错误分组一一对应）。"""
        mode = self.correlation_level
        count = self.agent_count
        if mode in {"none", "off", ""}:
            return None  # 默认轮转 4 个互不相同的专家画像
        if count <= 1:
            return None
        base = default_capability_profiles()
        if mode == "independent":
            # 画像互不相同：Agent i 用画像 i（数量超出时轮转复用，但大概率不重复）
            return [base[i % len(base)] for i in range(count)]
        if mode == "pairs":
            first, second = base[0], base[1]
            pattern = [first, first, second, second]
            return [dict(pattern[i % len(pattern)]) for i in range(count)]
        if mode == "all":
            return [dict(base[0]) for _ in range(count)]
        return None

    def _make_pool(self) -> list[TaskAgent]:
        """构造 Agent 池。三组共用同一套能力画像与种子。"""
        return make_agent_pool(
            self.agent_count,
            llm_factory=lambda i: self._make_llm(competence=0.7, salt=i + 1),
            profiles=self.resolve_profiles(),
            error_groups=self.resolve_error_groups(),
        )

    def _make_ledger(self):
        if not self.use_chain:
            return NullLedger()
        try:
            return get_ledger(enabled=True)
        except ChainError as exc:
            # 链不可用时**不静默降级**：明确报错并终止，
            # 否则实验报告里的「已上链」就是假的。
            raise SystemExit(
                f"[错误] 启用了上链但链不可用：\n{exc}\n"
                f"       若暂时不需要上链，请加 --no-chain 或在 .env 里设 CHAIN_ENABLED=false"
            ) from exc

    # -- A 组：单 LLM 直答 -------------------------------------------------

    def run_group_a(self) -> GroupResult:
        """基线组：一个 LLM 独立完成全部题目，无拆解、无协同。

        它的能力值取 Agent 池各领域能力的平均——一个"通才"而非"专家"，
        这正是单体大模型面对跨领域复合任务时的真实处境。
        """
        result = GroupResult("a", GROUP_LABELS["a"])
        result.provider = self.provider
        result.model = self.model
        pool_profile = self._make_pool()
        generalist_competence = statistics.fmean(
            statistics.fmean(a.capabilities.values()) for a in pool_profile
        )
        logger.info("A 组：单 LLM，通才能力值 %.3f", generalist_competence)

        llm = self._make_llm(competence=generalist_competence, salt=0)
        start = time.perf_counter()

        for q in self.questions:
            t0 = time.perf_counter()
            response = llm.generate(
                f"【领域】{q.domain}\n【难度】{q.difficulty}/5\n【任务】{q.question}\n\n请直接给出答案。",
                system="你是一个通用助手。请直接给出答案，不要解释推理过程。",
                competence=generalist_competence,
                difficulty=q.difficulty,
            )
            elapsed = (time.perf_counter() - t0) * 1000
            framework_elapsed = (
                elapsed
                if getattr(llm, "provider", "") == "mock"
                else max(0.0, elapsed - response.latency_ms)
            )

            result.records.append(
                QuestionRecord(
                    group="a",
                    question_id=q.id,
                    domain=q.domain,
                    difficulty=q.difficulty,
                    correct=is_correct(response.clean_text, q.expected_tokens),
                    hallucinations=int(response.hallucinated),
                    # 与 B/C 组同口径：LLM 耗时 + 框架开销（A 组几乎没有框架开销）
                    latency_ms=response.latency_ms + framework_elapsed,
                    llm_latency_ms=response.latency_ms,
                    framework_latency_ms=framework_elapsed,
                    subtask_count=1,
                    agent_ids="single-llm",
                    chain_tx=0,
                    chain_gas=0,
                    final_answer=response.clean_text,
                )
            )

        result.wall_clock_s = time.perf_counter() - start
        result.agent_stats = [
            {"agent_id": "single-llm", "task_count": len(self.questions),
             "completed": len(self.questions), "failed": 0,
             "hallucinations": result.hallucinations, "success_rate": 1.0,
             "avg_latency_ms": round(result.avg_latency_ms, 2), "capabilities": {}}
        ]
        return result

    # -- B / C 组：多智能体 -----------------------------------------------

    def _run_multi_agent(
        self, group: str, use_scoring: bool, use_chain: bool, transport: str
    ) -> GroupResult:
        label = GROUP_LABELS[group]
        if group == "c" and self.scheduling == "adaptive":
            label = "C 完整BlkMaC（自适应调度）"
        result = GroupResult(group, label)
        result.provider = self.provider
        result.model = self.model
        result.scheduling = self.scheduling
        result.trust_aware = self.trust_aware
        result.difficulty_matching = self.difficulty_matching
        result.redundancy_budget = self.redundancy_budget
        result.correlation_aware = self.correlation_aware
        result.correlation_level = self.correlation_level
        result.method = self.method
        pool = self._make_pool()
        ledger = self._make_ledger() if use_chain else NullLedger()

        controller = ControllerAgent(
            llm=self._make_llm(competence=0.9, salt=999),  # 拆解器不参与作答，能力设高
            agents=pool,
            ledger=ledger,
            weights=ScoreWeights(),
            transport=transport,
            scheduling=self.scheduling,
            trust_aware=self.trust_aware,
            difficulty_matching=self.difficulty_matching,
            redundancy_budget=self.redundancy_budget,
            correlation_aware=self.correlation_aware,
            redundancy_strategy=self.redundancy_strategy,
        )
        if transport == "tcp":
            controller.wire_tcp()

        result.chain_enabled = bool(getattr(ledger, "enabled", False))
        start = time.perf_counter()

        try:
            for q in self.questions:
                trace = controller.run(
                    q.question,
                    question_id=q.id,
                    use_scoring=use_scoring,
                    upload_chain=use_chain,
                )
                receipts = trace.chain_receipts
                result.records.append(
                    QuestionRecord(
                        group=group,
                        question_id=q.id,
                        domain=q.domain,
                        difficulty=q.difficulty,
                        correct=is_correct(trace.final_answer, q.expected_tokens),
                        hallucinations=trace.hallucination_count,
                        latency_ms=trace.total_latency_ms,
                        llm_latency_ms=trace.llm_latency_ms,
                        framework_latency_ms=trace.framework_latency_ms,
                        subtask_count=len(trace.subtasks),
                        agent_ids="|".join(a["agent_id"] for a in trace.assignments),
                        chain_tx=sum(1 for r in receipts if r.get("status") == "success"),
                        chain_gas=sum(int(r.get("gas_used") or 0) for r in receipts),
                        final_answer=trace.final_answer,
                        redundant_calls=len(trace.redundancy),
                    )
                )
                result.traces.append(trace.to_dict())
        finally:
            if transport == "tcp":
                controller.shutdown()

        result.wall_clock_s = time.perf_counter() - start
        result.agent_stats = controller.agent_stats()
        return result

    def run_group_b(self) -> GroupResult:
        """对照组：普通多智能体。固定轮询分配，无打分、无加密、无链。"""
        logger.info("B 组：普通多智能体（轮询分配）")
        return self._run_multi_agent("b", use_scoring=False, use_chain=False, transport="direct")

    def run_group_c(self) -> GroupResult:
        """实验组：完整 BlkMaC。动态打分 + 加密通信 + 上链存证。"""
        logger.info(
            "C 组：完整 BlkMaC（调度=%s，上链=%s，传输=%s）",
            self.scheduling,
            self.use_chain,
            self.transport,
        )
        return self._run_multi_agent(
            "c", use_scoring=True, use_chain=self.use_chain, transport=self.transport
        )

    def run(self, modes: Sequence[str]) -> dict[str, GroupResult]:
        runners = {"a": self.run_group_a, "b": self.run_group_b, "c": self.run_group_c}
        return {m: runners[m]() for m in modes}


# ===========================================================================
# 输出：CSV / JSONL / 图表
# ===========================================================================


def export_results(results: dict[str, GroupResult], out_dir: Path) -> dict[str, Path]:
    """导出 CSV 与 JSONL。"""
    import pandas as pd

    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    summary_df = pd.DataFrame([r.summary() for r in results.values()])
    path = out_dir / "metrics.csv"
    summary_df.to_csv(path, index=False, encoding="utf-8-sig")  # BOM 让 Excel 正确识别中文
    written["metrics"] = path

    per_q = pd.DataFrame(
        [
            {
                "group": r.group,
                "label": r.label,
                "provider": r.provider,
                "model": r.model,
                "question_id": rec.question_id,
                "domain": rec.domain,
                "difficulty": rec.difficulty,
                "correct": int(rec.correct),
                "hallucinations": rec.hallucinations,
                "latency_ms": round(rec.latency_ms, 2),
                "llm_latency_ms": round(rec.llm_latency_ms, 2),
                "framework_latency_ms": round(rec.framework_latency_ms, 2),
                "subtask_count": rec.subtask_count,
                "agent_ids": rec.agent_ids,
                "chain_tx": rec.chain_tx,
                "chain_gas": rec.chain_gas,
                "final_answer": rec.final_answer,
            }
            for r in results.values()
            for rec in r.records
        ]
    )
    path = out_dir / "per_question.csv"
    per_q.to_csv(path, index=False, encoding="utf-8-sig")
    written["per_question"] = path

    load_df = pd.DataFrame(
        [
            {"group": r.group, "label": r.label, **s}
            for r in results.values()
            for s in r.agent_stats
        ]
    )
    path = out_dir / "agent_load.csv"
    load_df.to_csv(path, index=False, encoding="utf-8-sig")
    written["agent_load"] = path

    path = out_dir / "trace.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for r in results.values():
            for trace in r.traces:
                fh.write(json.dumps({"group": r.group, **trace}, ensure_ascii=False) + "\n")
    written["trace"] = path

    return written


def _setup_chinese_font() -> bool:
    """配置中文字体。找不到时退回英文标签，避免图上全是方框。"""
    import matplotlib
    from matplotlib import font_manager

    installed = {f.name for f in font_manager.fontManager.ttflist}
    for candidate in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "PingFang SC",
                      "WenQuanYi Zen Hei", "Source Han Sans SC"):
        if candidate in installed:
            matplotlib.rcParams["font.sans-serif"] = [candidate]
            matplotlib.rcParams["axes.unicode_minus"] = False
            return True
    logger.warning("未找到中文字体，图表改用英文标签")
    return False


def plot_results(results: dict[str, GroupResult], out_dir: Path) -> list[Path]:
    """出三张对照图。"""
    import matplotlib

    matplotlib.use("Agg")  # 无显示环境也能出图
    import matplotlib.pyplot as plt

    has_cjk = _setup_chinese_font()
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    ordered = [results[k] for k in ("a", "b", "c") if k in results]
    labels = [r.label if has_cjk else f"Group {r.group.upper()}" for r in ordered]
    colors = ["#8c8c8c", "#4c78a8", "#e45756"][: len(ordered)]

    def t(zh: str, en: str) -> str:
        return zh if has_cjk else en

    # --- 图1：正确率 + 幻觉次数 ---
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    acc = [r.accuracy * 100 for r in ordered]
    bars = ax1.bar(labels, acc, color=colors)
    ax1.set_ylabel(t("正确率 (%)", "Accuracy (%)"))
    ax1.set_title(t("三组正确率对比", "Accuracy by Group"))
    ax1.set_ylim(0, 105)
    ax1.grid(axis="y", alpha=0.3)
    for bar, v in zip(bars, acc):
        ax1.text(bar.get_x() + bar.get_width() / 2, v + 2, f"{v:.1f}%", ha="center")

    hal = [r.hallucinations for r in ordered]
    bars = ax2.bar(labels, hal, color=colors)
    ax2.set_ylabel(t("幻觉次数", "Hallucination Count"))
    ax2.set_title(t("三组幻觉次数对比", "Hallucinations by Group"))
    # 留出 15% 顶部空间，否则最高柱的数值标注会被标题挡住或跑出画布
    ax2.set_ylim(0, max(hal + [1]) * 1.15)
    ax2.grid(axis="y", alpha=0.3)
    for bar, v in zip(bars, hal):
        ax2.text(
            bar.get_x() + bar.get_width() / 2,
            v + max(hal + [1]) * 0.02,
            str(v),
            ha="center",
            va="bottom",
        )

    fig.tight_layout()
    path = out_dir / "fig_accuracy_hallucination.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    written.append(path)

    # --- 图2：各 Agent 任务量分布 ---
    multi = [r for r in ordered if r.group in {"b", "c"}]
    if multi:
        fig, ax = plt.subplots(figsize=(10, 5))
        agent_names = sorted({name for r in multi for name in r.agent_loads})
        width = 0.8 / len(multi)
        for i, r in enumerate(multi):
            loads = [r.agent_loads.get(n, 0) for n in agent_names]
            offsets = [j + i * width - 0.4 + width / 2 for j in range(len(agent_names))]
            ax.bar(
                offsets,
                loads,
                width=width,
                label=f"{r.label if has_cjk else 'Group ' + r.group.upper()}"
                f" (var={r.load_variance:.2f})",
                color=colors[ordered.index(r)],
            )
        ax.set_xticks(range(len(agent_names)))
        ax.set_xticklabels(agent_names, rotation=15)
        ax.set_ylabel(t("承接任务数", "Tasks Handled"))
        # 标题不写「方差越小越好」：轮询天然方差为 0 但完全不看领域，
        # 动态打分为专业对口牺牲了一部分均衡度，这是权衡而非优劣。
        ax.set_title(
            t("各智能体任务量分布（B 轮询绝对均匀但不看领域；C 打分偏向专业对口）",
              "Task Distribution (B: uniform but domain-blind; C: specialty-driven)")
        )
        ax.legend()
        ax.grid(axis="y", alpha=0.3)
        fig.tight_layout()
        path = out_dir / "fig_agent_load.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        written.append(path)

    # --- 图3：耗时 ---
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    lat = [r.avg_latency_ms for r in ordered]
    bars = ax1.bar(labels, lat, color=colors)
    ax1.set_ylabel(t("平均每题耗时 (ms)", "Avg Latency per Question (ms)"))
    ax1.set_title(t("三组平均耗时对比", "Average Latency by Group"))
    ax1.grid(axis="y", alpha=0.3)
    for bar, v in zip(bars, lat):
        ax1.text(bar.get_x() + bar.get_width() / 2, v, f"{v:.0f}", ha="center", va="bottom")

    for r, color in zip(ordered, colors):
        ax2.plot(
            [rec.question_id for rec in r.records],
            [rec.latency_ms for rec in r.records],
            marker="o",
            markersize=3,
            label=r.label if has_cjk else f"Group {r.group.upper()}",
            color=color,
        )
    ax2.set_ylabel(t("耗时 (ms)", "Latency (ms)"))
    ax2.set_title(t("逐题耗时曲线", "Per-question Latency"))
    ax2.tick_params(axis="x", rotation=90, labelsize=7)
    ax2.legend()
    ax2.grid(alpha=0.3)

    fig.tight_layout()
    path = out_dir / "fig_latency.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    written.append(path)

    return written


def run_ablation_experiment(
    questions: Sequence[Question],
    agent_count: int | None = None,
    seed: int | None = None,
    use_chain: bool = False,
    transport: str = "direct",
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> dict[str, GroupResult]:
    """运行 2.1 消融实验，各变体复用相同题库、种子和 Agent 初始画像。"""
    variants = build_ablation_variants(use_chain)
    results: dict[str, GroupResult] = {}
    for variant in variants:
        runner = ExperimentRunner(
            questions=questions,
            agent_count=agent_count,
            seed=seed,
            use_chain=variant.use_chain,
            transport=transport,
            scheduling=variant.scheduling,
            provider=provider,
            model=model,
            base_url=base_url,
            trust_aware=variant.trust_aware,
            difficulty_matching=variant.difficulty_matching,
        )
        result = runner.run(["c"])["c"]
        result.group = variant.key
        result.label = variant.label
        results[variant.key] = result
    return results


def print_ablation_report(results: dict[str, GroupResult]) -> None:
    """打印消融实验汇总。"""
    print()
    print("=" * 104)
    print("  BlkMaC 2.1 消融实验结果")
    print("=" * 104)
    print(f"{'变体':<28}{'正确率':>10}{'幻觉':>8}{'均耗时(ms)':>14}{'负载方差':>12}{'链上交易':>12}")
    print("-" * 104)
    for result in results.values():
        print(
            f"{result.label:<28}{result.accuracy * 100:>9.1f}%"
            f"{result.hallucinations:>8}{result.avg_latency_ms:>14.1f}"
            f"{result.load_variance:>12.2f}{result.chain_tx_total:>12}"
        )
    print("-" * 104)
    print("指标说明：完整方法作为参照；每个变体只移除一个模块，其他配置保持一致。")
    print("=" * 104)
    print()


def print_report(results: dict[str, GroupResult]) -> None:
    """终端打印对照报告。"""
    print()
    print("=" * 88)
    print("  BlkMaC 三组对照实验结果")
    print("=" * 88)
    header = f"{'组别':<18}{'题数':>6}{'正确':>6}{'正确率':>9}{'幻觉':>7}{'均耗时(ms)':>13}{'负载方差':>10}{'链上交易':>10}"
    print(header)
    print("-" * 88)
    for key in ("a", "b", "c"):
        if key not in results:
            continue
        r = results[key]
        print(
            f"{r.label:<18}{r.total:>6}{sum(x.correct for x in r.records):>6}"
            f"{r.accuracy * 100:>8.1f}%{r.hallucinations:>7}"
            f"{r.avg_latency_ms:>13.1f}{r.load_variance:>10.2f}{r.chain_tx_total:>10}"
        )
    print("-" * 88)

    if "b" in results and "c" in results:
        b, c = results["b"], results["c"]
        acc_gain = (c.accuracy - b.accuracy) * 100
        hal_drop = b.hallucinations - c.hallucinations
        print()
        print("  C 组 vs B 组（动态打分 vs 轮询分配）：")
        print(f"    正确率   {b.accuracy * 100:.1f}%  ->  {c.accuracy * 100:.1f}%   ({acc_gain:+.1f} 个百分点)")
        print(f"    幻觉     {b.hallucinations}  ->  {c.hallucinations}   ({-hal_drop:+d} 次)")
        print(f"    负载方差 {b.load_variance:.2f}  ->  {c.load_variance:.2f}")

        if c.load_variance > b.load_variance:
            print()
            print("  [注意] C 组负载方差高于 B 组，这是如实结果，不是缺陷掩盖：")
            print("         轮询分配天然绝对均匀（方差趋近 0），但完全不看领域；")
            if "自适应" in c.label:
                print("         自适应调度在历史质量、难度匹配和响应速度之外仍保留专业匹配，")
                print("         当前结果体现了准确率与负载均衡之间的取舍。")
            else:
                print("         静态能力打分为了「专业对口」会把任务集中到专家 Agent 上，")
                print(f"         当前权重 w_cap={settings.SCORE_W_CAPABILITY} > "
                      f"w_load={settings.SCORE_W_LOAD}，因此倾向准确率而非均衡。")
            print("         可调高负载权重，在准确率与均衡之间重新取舍。")

    if "c" in results and not results["c"].chain_enabled:
        print()
        print("  [提示] C 组本次未启用上链（CHAIN_ENABLED=false 或 --no-chain）。")
        print("         链上存证相关指标为 0。【待 Ganache 私有链部署启动后，再进行联调】")
    print("=" * 88)
    print()


# ===========================================================================
# CLI
# ===========================================================================


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="BlkMaC 三组对照实验",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--mode", default="all", help="要跑的组：a / b / c / all / 组合如 bc")
    p.add_argument("--dataset", default=None, help="题库路径，默认读 .env 的 DATASET_PATH")
    p.add_argument("--agents", type=int, default=None, help="Task Agent 数量")
    p.add_argument("--seed", type=int, default=None, help="随机种子，保证结果可复现")
    p.add_argument("--out", default=None, help="输出目录，默认 .env 的 RESULTS_DIR")
    p.add_argument("--provider", default=None, help="LLM 后端：mock / openai / anthropic")
    p.add_argument("--model", default=None, help="模型名，覆盖 .env 的 LLM_MODEL")
    p.add_argument(
        "--base-url",
        default=None,
        help="真实后端地址，覆盖 .env 的 LLM_BASE_URL（OpenAI 兼容端点可复用）",
    )
    p.add_argument(
        "--transport",
        choices=["direct", "tcp"],
        default="direct",
        help="C 组的通信方式。tcp 走完整 RSA 加密链路（慢很多），direct 进程内直调",
    )
    p.add_argument(
        "--scheduling",
        choices=["static", "adaptive"],
        default="static",
        help="C 组调度算法：static 为现有能力评分，adaptive 为自适应评分",
    )
    p.add_argument(
        "--trust-aware",
        action="store_true",
        help="在 adaptive 调度中启用 Trust Score（Adaptive Trust-aware Scheduling）",
    )
    chain = p.add_mutually_exclusive_group()
    chain.add_argument("--chain", dest="chain", action="store_true", help="强制启用上链")
    chain.add_argument("--no-chain", dest="chain", action="store_false", help="强制关闭上链")
    p.set_defaults(chain=None)
    p.add_argument("--no-plot", action="store_true", help="只出 CSV，不画图")
    p.add_argument("--limit", type=int, default=None, help="只跑前 N 题（快速冒烟）")
    p.add_argument(
        "--ablation",
        action="store_true",
        help="运行 2.1 消融实验：完整、去 Trust、去自适应、去区块链、去难度匹配",
    )
    # --- 相关错误感知的可靠调度与验证（PROPOSAL_CORRELATION.md）---
    p.add_argument(
        "--redundancy-budget",
        dest="redundancy_budget",
        type=int,
        default=None,
        help="冗余验证预算（额外 LLM 调用次数）；默认读 REDUNDANCY_BUDGET，0 = 关闭",
    )
    p.add_argument(
        "--method",
        default="baseline",
        choices=("baseline", "random", "reputation", "proposed"),
        help=(
            "冗余调度方法：baseline=无冗余 / random=随机加验证 / "
            "reputation=信誉加权（忽略相关性，即 B2）/ proposed=相关性感知"
        ),
    )
    p.add_argument(
        "--correlation-level",
        dest="correlation_level",
        default="none",
        choices=("none", "independent", "pairs", "all"),
        help=(
            "错误相关性档位（同时决定能力画像与错误潜因子分组）："
            "none=不启用（默认，行为与既有 C 组一致）/ independent=异画像独立 / "
            "pairs=2 个同画像（中等相关）/ all=全部同画像（高度相关）"
        ),
    )
    return p


def parse_modes(mode: str) -> list[str]:
    mode = mode.lower().strip()
    if mode == "all":
        return ["a", "b", "c"]
    modes = [c for c in mode if c in {"a", "b", "c"}]
    if not modes:
        raise SystemExit(f"[错误] --mode 取值非法：{mode!r}（可用 a / b / c / all / bc 等组合）")
    return modes


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    modes = parse_modes(args.mode)

    questions = load_dataset(args.dataset)
    if args.limit:
        questions = questions[: args.limit]

    if args.ablation:
        results = run_ablation_experiment(
            questions=questions,
            agent_count=args.agents,
            seed=args.seed,
            use_chain=args.chain is True,
            transport=args.transport,
            provider=args.provider,
            model=args.model,
            base_url=args.base_url,
        )
        print_ablation_report(results)
        out_dir = Path(args.out) if args.out else settings.RESULTS_DIR
        written = export_results(results, out_dir)
        print(f"数据已导出到 {out_dir}：")
        for name, path in written.items():
            print(f"    {name:<14} {path.name}")
        return 0

    runner = ExperimentRunner(
        questions=questions,
        agent_count=args.agents,
        seed=args.seed,
        use_chain=args.chain,
        transport=args.transport,
        scheduling=args.scheduling,
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        trust_aware=args.trust_aware,
        redundancy_budget=(
            settings.REDUNDANCY_BUDGET
            if args.redundancy_budget is None
            else args.redundancy_budget
        ),
        # baseline=无冗余；proposed=相关性感知；reputation=B2（忽略相关性）
        correlation_aware=args.method != "reputation",
        correlation_level=args.correlation_level,
        method=args.method,
    )

    print()
    print(f"题库 {len(questions)} 题 | Agent {runner.agent_count} 个 | 种子 {runner.seed} "
          f"| LLM {runner.provider}/{runner.model} | 调度 {runner.scheduling} "
          f"| Trust-aware {runner.trust_aware} "
          f"| 传输 {runner.transport} "
          f"| 上链 {runner.use_chain}")

    results = runner.run(modes)
    print_report(results)

    out_dir = Path(args.out) if args.out else settings.RESULTS_DIR
    written = export_results(results, out_dir)
    print(f"数据已导出到 {out_dir}：")
    for name, path in written.items():
        print(f"    {name:<14} {path.name}")

    if not args.no_plot:
        figures = plot_results(results, out_dir)
        print("图表：")
        for path in figures:
            print(f"    {path.name}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
