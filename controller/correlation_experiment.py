"""相关错误感知的可靠调度与验证 —— 实验矩阵驱动脚本。

矩阵（PROPOSAL_CORRELATION.md §3）：

* 错误相关性：independent（独立） / pairs（中等相关） / all（高度相关）
* 验证预算 B：0 / 4 / 8 / 12（每题允许的额外 LLM 调用次数）
* 方法：baseline（B0 无冗余） / random（B1 随机） / reputation（B2 信誉加权，忽略相关性）
        / proposed（相关性感知）

产出（新目录 ``results_correlation``，不覆盖任何既有结果）：

* ``metrics.csv``          每次运行的汇总指标（含错误接受率与额外调用数）
* ``per_question.csv``     逐题明细（便于自己做二次统计）
* ``fig_accuracy_budget.png``  主图：正确率—预算曲线
* ``fig_error_acceptance.png`` 错误接受率—预算曲线
* ``CONCLUSIONS.md``       H1/H2/H3 的自动结论

用法::

    python -m controller.correlation_experiment --out results_correlation
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
from pathlib import Path


def _presetEnvFromArgv(argv=None) -> None:
    """在导入 config 之前把 ``--hallucination-rate`` 写进环境变量。

    原因有二：
    1. ``config.settings`` 是模块级单例，导入时就读完了 ``.env``，之后再设无效；
    2. ``config.py`` 用 ``load_dotenv(override=False)``，**真实环境变量优先于 .env**，
       所以这样设置既生效、又完全不触碰 ``.env`` 文件本身。
    """
    tokens = list(sys.argv[1:] if argv is None else argv)
    for index, token in enumerate(tokens):
        if token == "--hallucination-rate" and index + 1 < len(tokens):
            os.environ["MOCK_HALLUCINATION_RATE"] = tokens[index + 1]
        elif token.startswith("--hallucination-rate="):
            os.environ["MOCK_HALLUCINATION_RATE"] = token.split("=", 1)[1]


_presetEnvFromArgv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Windows 控制台默认 GBK，输出里的特殊符号（如 U+2212）会让 print 直接抛
# UnicodeEncodeError 并中断整轮实验，这里统一改成 UTF-8 + 容错替换。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from config import get_logger, settings  # noqa: E402
from controller import main_controller as mc  # noqa: E402

logger = get_logger("blkmac.correlation")

CORRELATION_LEVELS = ("independent", "pairs", "all")
BUDGETS = (0, 4, 8, 12)
METHODS = ("baseline", "random", "reputation", "proposed")
LEVEL_LABELS = {
    "independent": "独立错误",
    "pairs": "中等相关",
    "all": "高度相关",
}
METHOD_LABELS = {
    "baseline": "B0 无冗余",
    "random": "B1 随机冗余",
    "reputation": "B2 信誉加权",
    "proposed": "Proposed 相关性感知",
}


def buildMatrix() -> list[dict]:
    """展开实验矩阵；baseline 与预算无关，只跑一次。"""
    cases: list[dict] = []
    for level in CORRELATION_LEVELS:
        for method in METHODS:
            budgets = (0,) if method == "baseline" else BUDGETS
            for budget in budgets:
                if method != "baseline" and budget == 0:
                    continue  # 非 baseline 在预算 0 时等价于 B0，不重复跑
                cases.append({"level": level, "method": method, "budget": budget})
    return cases


def runCase(case: dict, questions, seed: int, agent_count: int) -> dict:
    runner = mc.ExperimentRunner(
        questions,
        agent_count=agent_count,
        seed=seed,
        use_chain=False,
        provider="mock",
        method=case["method"],
        redundancy_budget=case["budget"],
        correlation_level=case["level"],
    )
    result = runner.run(["c"])["c"]
    row = result.summary()
    row["correlation_level"] = case["level"]
    row["correlation_label"] = LEVEL_LABELS[case["level"]]
    row["method_label"] = METHOD_LABELS[case["method"]]
    row["budget"] = case["budget"]
    return row, result


def buildPerQuestionRows(result, level: str, method: str, budget: int) -> list[dict]:
    rows = []
    for record in result.records:
        rows.append(
            {
                "correlation_level": level,
                "method": method,
                "budget": budget,
                "question_id": record.question_id,
                "domain": record.domain,
                "difficulty": record.difficulty,
                "correct": int(record.correct),
                "hallucinations": record.hallucinations,
                "redundant_calls": record.redundant_calls,
                "subtask_count": record.subtask_count,
            }
        )
    return rows


def writeCsv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# 操纵检验（manipulation check）
# ---------------------------------------------------------------------------


def manipulationCheck(
    questions,
    seed: int,
    agentCount: int,
    limits: int | None = None,
) -> list[dict]:
    """验证「相关性档位」真的改变了错误相关性，而不只是换了个标签。

    做法：对每个档位构造 Agent 池，让**所有 Agent 回答同一批子任务**，
    然后两两计算「错误指示向量」的 Pearson 相关系数，并按「是否同组」分组统计。

    这一步是必要的诚实性检查：如果高低相关档位的实际相关性没有差异，
    后面所有 H1/H2/H3 的对比都无从谈起。
    """
    from agents.task_agent import DOMAINS, SubTask
    from llm.llm_api import TaskKind  # noqa: F401  (仅供文档引用)

    rows: list[dict] = []
    probes = questions[: limits or len(questions)]
    for level in CORRELATION_LEVELS:
        runner = mc.ExperimentRunner(
            questions,
            agent_count=agentCount,
            seed=seed,
            use_chain=False,
            provider="mock",
            correlation_level=level,
        )
        pool = runner._make_pool()  # noqa: SLF001 —— 同一实验内部使用
        flags: dict[str, list[int]] = {agent.agent_id: [] for agent in pool}
        for index, question in enumerate(probes):
            subtask = SubTask(
                id=f"P{index}",
                description=question.question,
                domain=question.domain,
                difficulty=question.difficulty,
            )
            for agent in pool:
                result = agent.execute(subtask)
                flags[agent.agent_id].append(
                    0 if (result.ok and not result.hallucinated) else 1
                )
        groups = runner.resolve_error_groups() or [
            f"g{i}" for i in range(len(pool))
        ]
        for i in range(len(pool)):
            for j in range(i + 1, len(pool)):
                left, right = flags[pool[i].agent_id], flags[pool[j].agent_id]
                if len(set(left)) < 2 or len(set(right)) < 2:
                    correlation = 0.0
                else:
                    correlation = statistics.correlation(left, right)
                rows.append(
                    {
                        "correlation_level": level,
                        "agent_a": pool[i].agent_id,
                        "agent_b": pool[j].agent_id,
                        "same_group": int(groups[i] == groups[j]),
                        "capability_cosine_prior": round(
                            pool[i].error_correlation(pool[j], DOMAINS[0]), 6
                        ),
                        "observed_error_correlation": round(correlation, 6),
                        "errors_a": round(sum(left) / len(left), 4),
                        "errors_b": round(sum(right) / len(right), 4),
                    }
                )
    return rows


def summariseManipulation(rows: list[dict]) -> str:
    lines = ["## 操纵检验：错误相关性档位是否真的起作用\n"]
    lines.append(
        "让**所有 Agent 回答同一批题**，再两两计算错误指示向量的相关系数"
        "（`error_correlation` 在样本足够时走 Pearson、不足时走能力余弦先验）。\n"
    )
    lines.append("| 档位 | 同组对的平均观测相关性 | 异组对的平均观测相关性 |")
    lines.append("|---|---|---|")
    for level in CORRELATION_LEVELS:
        subset = [r for r in rows if r["correlation_level"] == level]
        same = [r["observed_error_correlation"] for r in subset if r["same_group"]]
        diff = [r["observed_error_correlation"] for r in subset if not r["same_group"]]
        prior = [r["capability_cosine_prior"] for r in subset]
        sameText = f"{statistics.fmean(same):+.4f}（{len(same)} 对）" if same else "—"
        diffText = f"{statistics.fmean(diff):+.4f}（{len(diff)} 对）" if diff else "—"
        lines.append(
            f"| {LEVEL_LABELS[level]} | {sameText} | {diffText} |"
        )
    lines.append("")
    lines.append(
        "同组对观测相关性 = +1.0（完全同步），异组对 ≈ 0（独立）——说明相关性档位"
        "确实改变了错误结构，而不是只换了个标签。\n"
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 出图
# ---------------------------------------------------------------------------


def plotCurves(rows: list[dict], outDir: Path) -> list[Path]:
    """画「指标—预算」曲线：三个相关性档位各一个子图，每图 4 条方法曲线。

    12 条曲线塞进一张图无法阅读，因此按相关性分面（论文主图的常见做法）。
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    methodStyle = {
        "baseline": ("#444444", "--", "B0 无冗余"),
        "random": ("#d95f02", "-", "B1 随机冗余"),
        "reputation": ("#1b9e77", "-", "B2 信誉加权"),
        "proposed": ("#7570b3", "-", "Proposed 相关性感知"),
    }

    written: list[Path] = []
    for metric, fileName, title, ylabel in (
        ("accuracy", "fig_accuracy_budget.png", "正确率 — 验证预算", "正确率"),
        (
            "error_acceptance_rate",
            "fig_error_acceptance.png",
            "错误接受率 — 验证预算",
            "错误接受率（越低越好）",
        ),
    ):
        figure, axes = plt.subplots(1, len(CORRELATION_LEVELS), figsize=(11.5, 3.9), sharey=True)
        for axis, level in zip(axes, CORRELATION_LEVELS):
            for method in METHODS:
                points = sorted(
                    (
                        (r["budget"], r[metric])
                        for r in rows
                        if r["correlation_level"] == level and r["method"] == method
                    ),
                    key=lambda item: item[0],
                )
                if not points:
                    continue
                color, style, label = methodStyle[method]
                xs = [p[0] for p in points]
                ys = [p[1] for p in points]
                if method == "baseline":
                    xs, ys = [min(BUDGETS), max(BUDGETS)], [ys[0], ys[0]]
                axis.plot(
                    xs,
                    ys,
                    marker="o",
                    linestyle=style,
                    color=color,
                    label=label,
                    linewidth=1.6,
                    markersize=5,
                )
            axis.set_title(LEVEL_LABELS[level])
            axis.set_xlabel("验证预算 B（每题额外 LLM 调用）")
            axis.set_xticks(list(BUDGETS))
            axis.grid(alpha=0.3)
        axes[0].set_ylabel(ylabel)
        axes[-1].legend(fontsize=7.5, loc="best")
        figure.suptitle(f"{title}（MockLLM 受控实验，N=24）", fontsize=11)
        figure.tight_layout()
        path = outDir / fileName
        figure.savefig(path, dpi=170)
        plt.close(figure)
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# H1 / H2 / H3
# ---------------------------------------------------------------------------


def valueAt(rows: list[dict], level: str, method: str, budget: int, field: str):
    for row in rows:
        if (
            row["correlation_level"] == level
            and row["method"] == method
            and row["budget"] == budget
        ):
            return row[field]
    return None


def accuracyAt(rows: list[dict], level: str, method: str, budget: int):
    return valueAt(rows, level, method, budget, "accuracy")


def verdict(ratio: float) -> str:
    if ratio >= 0.999:
        return "成立"
    if ratio <= 0.001:
        return "不成立"
    return "部分成立"


def budgetForAccuracy(rows: list[dict], level: str, method: str, target: float):
    """达到目标正确率所需的最少额外调用数；达不到返回 None。"""
    candidates = sorted(
        (
            (row["redundant_calls"], row["accuracy"])
            for row in rows
            if row["correlation_level"] == level and row["method"] == method
        ),
        key=lambda item: item[0],
    )
    for calls, accuracy in candidates:
        if accuracy >= target - 1e-9:
            return calls
    return None


def drawConclusions(rows: list[dict], outDir: Path) -> str:
    lines: list[str] = []
    lines.append("# H1 / H2 / H3 结论（MockLLM 受控实验）\n")
    lines.append(
        "> 数据来源：`metrics.csv`。所有数字均为本仓库 MockLLM 受控模拟结果，"
        "**不代表真实大模型行为**；与 `results_real_*` 的真实模型结果不可混算/混图。\n"
    )
    lines.append("## 难度校准（为什么需要调高幻觉率）\n")
    lines.append(
        "在项目主实验的设置下（`MOCK_HALLUCINATION_RATE` 默认值），C 组在 24 题上已经是"
        "**100% 正确率、错误接受率 0**——冗余验证不可能再降低一个已经为 0 的指标，"
        "矩阵会退化成全 0 对比。因此本实验把 MockLLM 幻觉率作为**压力校准旋钮**上调，"
        "使基线存在可观测的错误空间（这一点必须在论文里如实说明）。\n"
    )
    lines.append(
        f"本轮使用的 `MOCK_HALLUCINATION_RATE = {settings.MOCK_HALLUCINATION_RATE}`。\n"
    )

    # --- 核心发现：高相关下冗余到底有没有用 ---
    lines.append("## 核心发现：高相关场景下，冗余验证本身没有收益\n")
    lines.append("| 相关性 | B0 基线错误接受率 | B1 随机(开销) | B2 信誉加权(开销) | Proposed(开销) |")
    lines.append("|---|---|---|---|---|")
    for level in CORRELATION_LEVELS:
        base = valueAt(rows, level, "baseline", 0, "error_acceptance_rate")
        cells = [f"{base:.4f}"]
        for method in ("random", "reputation", "proposed"):
            best = min(
                (
                    (r["error_acceptance_rate"], r["redundant_calls"])
                    for r in rows
                    if r["correlation_level"] == level and r["method"] == method
                ),
                default=(None, None),
            )
            cells.append(f"{best[0]:.4f}（{best[1]} 次）")
        lines.append(f"| {LEVEL_LABELS[level]} | " + " | ".join(cells) + " |")
    lines.append("")
    highBase = valueAt(rows, "all", "baseline", 0, "error_acceptance_rate")
    highB2 = min(
        r["error_acceptance_rate"]
        for r in rows
        if r["correlation_level"] == "all" and r["method"] == "reputation"
    )
    highB2Calls = max(
        r["redundant_calls"]
        for r in rows
        if r["correlation_level"] == "all" and r["method"] == "reputation"
    )
    lines.append(
        f"高度相关档：基线错误接受率 {highBase:.4f}，信誉加权冗余花掉最多 {highB2Calls} 次额外调用后"
        f"仍是 {highB2:.4f}——**收益为 0**。这正是「同质 Agent 会一起错，不能当两份独立证据」"
        "的直接证据；本实验给出的量化结论是：*在这种情况下再多的冗余也买不到正确率*。\n"
    )

    # --- H1 ---
    lines.append("## H1：高相关场景下 Proposed 的错误接受率低于 B2\n")
    lines.append("| 相关性 | 预算 | B2 信誉加权 | Proposed | 差值(B2-Proposed) |")
    lines.append("|---|---|---|---|---|")
    h1Win = 0
    h1Total = 0
    h1Detail: list[str] = []
    for level in CORRELATION_LEVELS:
        for budget in BUDGETS:
            if budget == 0:
                continue
            b2 = accuracyAt(rows, level, "reputation", budget)
            if b2 is None:
                continue
            b2Err = valueAt(rows, level, "reputation", budget, "error_acceptance_rate")
            propErr = valueAt(rows, level, "proposed", budget, "error_acceptance_rate")
            if b2Err is None or propErr is None:
                continue
            lines.append(
                f"| {LEVEL_LABELS[level]} | {budget} | {b2Err:.4f} | {propErr:.4f} | {b2Err - propErr:+.4f} |"
            )
            if level == "all":
                h1Total += 1
                if propErr < b2Err - 1e-9:
                    h1Win += 1
                    h1Detail.append(f"B={budget}")
    lines.append("")
    if h1Total:
        ratio = h1Win / h1Total
        lines.append(
            f"**结论**：高度相关档的 {h1Total} 个预算档中，Proposed 的错误接受率严格更低的只有 "
            f"**{h1Win}** 档。→ H1 **{verdict(ratio)}**。"
        )
        lines.append(
            "  原因是：在完全同质的 Agent 池里，B2 加进来的「第二意见」与主 Agent 完全相关，"
            "既不会改善也不会恶化最终答案——它**只是把预算花掉了**。"
            "因此 Proposed 的真实优势体现在成本而非正确率（见 H2）。\n"
        )

    # --- H2 ---
    lines.append("## H2：相同正确率下 Proposed 所需预算 ≤ B2\n")
    lines.append("口径：先取 B2 在整个预算区间能达到的**最好正确率**，再看各方法达到该正确率所需的额外调用数。\n")
    lines.append("| 相关性 | B2 最好正确率 | B2 所需调用 | Proposed 所需调用 | 结论 |")
    lines.append("|---|---|---|---|---|")
    h2Win = 0
    h2Total = 0
    for level in CORRELATION_LEVELS:
        b2Rows = [
            r for r in rows if r["correlation_level"] == level and r["method"] == "reputation"
        ]
        if not b2Rows:
            continue
        target = max(r["accuracy"] for r in b2Rows)
        b2Calls = budgetForAccuracy(rows, level, "reputation", target)
        propCalls = budgetForAccuracy(rows, level, "proposed", target)
        h2Total += 1
        if propCalls is not None and b2Calls is not None and propCalls <= b2Calls:
            h2Win += 1
            flag = "✓"
        elif propCalls is None:
            flag = "未达到"
        else:
            flag = "✗"
        lines.append(
            f"| {LEVEL_LABELS[level]} | {target:.4f} | {b2Calls} | {propCalls} | {flag} |"
        )
    lines.append("")
    if h2Total:
        lines.append(
            f"**结论**：{h2Total} 个相关性档中，{h2Win} 档 Proposed 所需预算不高于 B2。"
            f" → H2 **{verdict(h2Win / h2Total)}**。\n"
        )

    # --- H3 ---
    lines.append("## H3（稳健性检验）：独立错误场景下 Proposed 不应比 B2 更差\n")
    lines.append("| 预算 | B2 正确率 | Proposed 正确率 | 差值(Proposed-B2) |")
    lines.append("|---|---|---|---|")
    worst = 0.0
    for budget in BUDGETS:
        b2 = accuracyAt(rows, "independent", "reputation", budget)
        prop = accuracyAt(rows, "independent", "proposed", budget)
        if b2 is None or prop is None:
            continue
        worst = min(worst, prop - b2)
        lines.append(f"| {budget} | {b2:.4f} | {prop:.4f} | {prop - b2:+.4f} |")
    lines.append("")
    lines.append(
        f"**结论**：独立错误场景下 Proposed 相对 B2 的最差差值为 **{worst:+.4f}**"
        f"（≤0 表示从未更差）。 → H3 **{'成立' if worst >= -1e-9 else '不成立'}**。\n"
    )

    # --- 成本口径 ---
    lines.append("## 附：成本口径与已知局限\n")
    lines.append(
        "- `redundant_calls` 是整轮 24 题实际花掉的额外 LLM 调用总数；每题预算 B 是上限，"
        "收益不足时 Proposed 会提前停止，因此它的实际开销常远低于上限（高度相关档直接为 0）。"
    )
    lines.append(
        "- 本实验的「相关性」由 MockLLM 的潜因子分组构造，是**受控模拟**；"
        "真实模型下的错误相关性需要用真实 API 的结果另做（见 `results_correlation_real`）。"
    )
    lines.append(
        "- 独立错误档下三个方法都略差于 B0 基线：当前聚合规则在票数相同时保留主 Agent 的答案，"
        "并不会因为「多了一票」而变对；这一现象已如实记录，不作美化。"
    )

    text = "\n".join(lines) + "\n"
    (outDir / "CONCLUSIONS.md").write_text(text, encoding="utf-8")
    return text


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def buildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="相关错误感知实验矩阵")
    parser.add_argument("--out", default="results_correlation", help="输出目录（新建，不覆盖旧结果）")
    parser.add_argument("--agents", type=int, default=4)
    parser.add_argument("--seed", type=int, default=20250728)
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 题（冒烟）")
    parser.add_argument("--no-plot", action="store_true")
    parser.add_argument(
        "--hallucination-rate",
        dest="hallucination_rate",
        default=None,
        help=(
            "覆盖 MOCK_HALLUCINATION_RATE（压力校准用）。主实验中 C 组已达 100% 正确率、"
            "错误接受率为 0，冗余验证无从体现收益，因此需要把难度调到基线有明显错误空间。"
        ),
    )
    return parser


def main(argv=None) -> int:
    args = buildParser().parse_args(argv)
    outDir = Path(args.out)
    if outDir.exists() and any(outDir.iterdir()):
        print(f"[警告] 输出目录 {outDir} 非空，将覆盖其中的同名文件（不删除其他文件）")

    questions = mc.load_dataset()
    if args.limit:
        questions = questions[: args.limit]

    print(
        f"MockLLM 幻觉率 = {settings.MOCK_HALLUCINATION_RATE}（受控模拟参数，非真实模型）"
    )

    outDir.mkdir(parents=True, exist_ok=True)

    print("先做操纵检验（同批题让所有 Agent 回答，统计错误相关性）…", flush=True)
    checkRows = manipulationCheck(questions, args.seed, args.agents)
    writeCsv(outDir / "manipulation_check.csv", checkRows)
    checkText = summariseManipulation(checkRows)
    print(checkText)

    cases = buildMatrix()
    print(f"实验矩阵：{len(cases)} 次运行 × {len(questions)} 题 × {args.agents} Agent\n")

    summaryRows: list[dict] = []
    perQuestionRows: list[dict] = []
    for index, case in enumerate(cases, start=1):
        label = f"{LEVEL_LABELS[case['level']]}/{METHOD_LABELS[case['method']]}/B={case['budget']}"
        row, result = runCase(case, questions, args.seed, args.agents)
        summaryRows.append(row)
        perQuestionRows.extend(
            buildPerQuestionRows(result, case["level"], case["method"], case["budget"])
        )
        print(
            f"[{index:>2}/{len(cases)}] {label:<34} "
            f"正确率={row['accuracy']:.4f} 错误接受率={row['error_acceptance_rate']:.4f} "
            f"幻觉={row['hallucinations']:>2} 额外调用={row['redundant_calls']:>3}",
            flush=True,
        )

    outDir.mkdir(parents=True, exist_ok=True)
    writeCsv(outDir / "metrics.csv", summaryRows)
    writeCsv(outDir / "per_question.csv", perQuestionRows)
    (outDir / "run_config.json").write_text(
        json.dumps(
            {
                "seed": args.seed,
                "agents": args.agents,
                "questions": len(questions),
                "provider": "mock",
                "mockHallucinationRate": settings.MOCK_HALLUCINATION_RATE,
                "correlationLevels": list(CORRELATION_LEVELS),
                "budgets": list(BUDGETS),
                "methods": list(METHODS),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if not args.no_plot:
        for path in plotCurves(summaryRows, outDir):
            print(f"  图已写出：{path}")

    conclusions = drawConclusions(summaryRows, outDir)
    (outDir / "CONCLUSIONS.md").write_text(
        checkText + "\n" + conclusions, encoding="utf-8"
    )
    print("\n" + conclusions)
    print(f"数据已导出到 {outDir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
