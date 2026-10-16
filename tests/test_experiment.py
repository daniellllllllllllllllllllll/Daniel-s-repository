"""controller 对照实验测试：题库完整性、评分逻辑、三组可复现性与产出文件。"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from controller import main_controller as mc


class TestDataset(unittest.TestCase):
    """题库是三组实验的共同输入，结构错了整个实验就没意义。"""

    @classmethod
    def setUpClass(cls):
        cls.questions = mc.load_dataset()

    def test_dataset_loads(self):
        self.assertGreaterEqual(len(self.questions), 20)

    def test_ids_unique(self):
        ids = [q.id for q in self.questions]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_question_has_answer(self):
        for q in self.questions:
            self.assertTrue(q.answer.strip(), f"{q.id} 缺少标准答案")

    def test_domains_valid(self):
        from agents.task_agent import DOMAINS

        for q in self.questions:
            self.assertIn(q.domain, DOMAINS, f"{q.id} 领域非法")
            for p in q.parts:
                self.assertIn(p.get("domain", q.domain), DOMAINS, f"{q.id} 子任务领域非法")

    def test_difficulty_in_range(self):
        for q in self.questions:
            self.assertTrue(1 <= q.difficulty <= 5, f"{q.id} 难度越界")

    def test_composite_questions_exist(self):
        """必须有复合题，否则考察不出「拆解 + 分派」的价值。"""
        multi = [q for q in self.questions if len(q.parts) > 1]
        self.assertGreaterEqual(len(multi), 5, "复合题太少，实验区分度不足")

    def test_composite_answer_contains_all_part_answers(self):
        """整题答案必须覆盖各子答案，否则 A 组永远判错，对比不公平。"""
        for q in self.questions:
            if len(q.parts) > 1:
                for token in q.expected_tokens:
                    self.assertTrue(
                        mc.is_correct(q.answer, [token]),
                        f"{q.id} 的整题答案 {q.answer!r} 未包含子答案 {token!r}",
                    )

    def test_expected_tokens(self):
        simple = next(q for q in self.questions if len(q.parts) <= 1)
        self.assertEqual(simple.expected_tokens, [simple.answer])

    def test_to_subtasks_carries_expected(self):
        q = next(q for q in self.questions if len(q.parts) > 1)
        subs = q.to_subtasks()
        self.assertEqual(len(subs), len(q.parts))
        self.assertTrue(all(s.expected for s in subs))
        self.assertTrue(all(s.parent_id == q.id for s in subs))

    def test_missing_dataset_raises(self):
        with self.assertRaises(SystemExit):
            mc.load_dataset("test_dataset/no_such_file.json")


class TestScoring(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(mc.is_correct("391", ["391"]))

    def test_answer_embedded_in_sentence(self):
        self.assertTrue(mc.is_correct("答案是 391 。", ["391"]))

    def test_punctuation_and_case_insensitive(self):
        self.assertTrue(mc.is_correct("O(N LOG N)", ["O(nlogn)"]))
        self.assertTrue(mc.is_correct("s == s[::-1]", ["s==s[::-1]"]))

    def test_all_tokens_required(self):
        """复合题只答对一半不算对。"""
        self.assertTrue(mc.is_correct("100；北京", ["100", "北京"]))
        self.assertFalse(mc.is_correct("100", ["100", "北京"]))

    def test_wrong_answer_rejected(self):
        self.assertFalse(mc.is_correct("392", ["391"]))

    def test_empty_answer_rejected(self):
        self.assertFalse(mc.is_correct("", ["391"]))


class TestKnowledgeBaseBuilders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.questions = mc.load_dataset()

    def test_kb_covers_whole_questions_and_parts(self):
        kb = mc.build_knowledge_base(self.questions)
        for q in self.questions:
            self.assertIn(q.question, kb)
            for p in q.parts:
                self.assertIn(p["description"], kb)

    def test_plan_only_for_composite(self):
        plan = mc.build_decomposition_plan(self.questions)
        for q in self.questions:
            if q.parts:
                self.assertIn(q.question, plan)
                self.assertEqual(len(plan[q.question]), len(q.parts))

    def test_plan_entries_have_required_fields(self):
        plan = mc.build_decomposition_plan(self.questions)
        for parts in plan.values():
            for p in parts:
                self.assertTrue({"id", "description", "domain", "difficulty"} <= set(p))


class TestExperimentRunner(unittest.TestCase):
    """三组实验的运行与指标。用前 8 题跑，保证测试速度。"""

    @classmethod
    def setUpClass(cls):
        # 必须同时包含单任务题与复合题：前者验证基本流程，
        # 后者才能验证「拆解 + 分派」这条核心链路。
        # 直接取前 N 题会全是单任务题（复合题从 Q15 才开始）。
        pool = mc.load_dataset()
        simple = [q for q in pool if len(q.parts) <= 1][:4]
        composite = [q for q in pool if len(q.parts) > 1][:4]
        cls.questions = simple + composite

        cls.runner = mc.ExperimentRunner(
            cls.questions, agent_count=4, seed=20250728, use_chain=False
        )
        cls.results = cls.runner.run(["a", "b", "c"])

    def test_fixture_contains_composite_questions(self):
        """守护上面的取题逻辑：若题库结构变化导致取不到复合题，这里先失败。"""
        self.assertGreaterEqual(
            len([q for q in self.questions if len(q.parts) > 1]), 1
        )

    def test_all_groups_ran(self):
        self.assertEqual(set(self.results), {"a", "b", "c"})

    def test_each_group_covers_all_questions(self):
        for group in self.results.values():
            self.assertEqual(group.total, len(self.questions))

    def test_accuracy_in_range(self):
        for group in self.results.values():
            self.assertTrue(0.0 <= group.accuracy <= 1.0)

    def test_chain_disabled_reports_zero_tx(self):
        c = self.results["c"]
        self.assertFalse(c.chain_enabled)
        self.assertEqual(c.chain_tx_total, 0)
        self.assertEqual(c.chain_gas_total, 0)

    def test_group_c_beats_group_b_on_hallucinations(self):
        """框架的核心主张：按能力派单比轮询派单幻觉更少。"""
        self.assertLessEqual(
            self.results["c"].hallucinations, self.results["b"].hallucinations
        )

    def test_group_c_accuracy_not_worse_than_b(self):
        self.assertGreaterEqual(self.results["c"].accuracy, self.results["b"].accuracy)

    def test_multi_agent_decomposes_composite_questions(self):
        """B/C 组必须真的把复合题拆开，否则拆解模块等于没生效。"""
        composite_ids = {q.id for q in self.questions if len(q.parts) > 1}
        self.assertTrue(composite_ids, "测试夹具里没有复合题，这条断言形同虚设")
        for group in ("b", "c"):
            for rec in self.results[group].records:
                if rec.question_id in composite_ids:
                    self.assertGreater(
                        rec.subtask_count, 1, f"{group} 组未拆解 {rec.question_id}"
                    )

    def test_group_a_never_decomposes(self):
        for rec in self.results["a"].records:
            self.assertEqual(rec.subtask_count, 1)

    def test_adaptive_scheduler_runs_and_records_breakdown(self):
        runner = mc.ExperimentRunner(
            self.questions[:2], agent_count=4, seed=20250728,
            use_chain=False, scheduling="adaptive"
        )
        result = runner.run(["c"])["c"]
        self.assertIn("自适应调度", result.label)
        self.assertTrue(result.traces)
        self.assertTrue(
            all("score_breakdown" in item for item in result.traces[0]["assignments"])
        )

    def test_trust_aware_scheduler_is_recorded(self):
        runner = mc.ExperimentRunner(
            self.questions[:2], agent_count=4, seed=20250728,
            use_chain=False, scheduling="adaptive", trust_aware=True
        )
        result = runner.run(["c"])["c"]
        self.assertTrue(result.trust_aware)
        self.assertEqual(result.scheduling, "adaptive")
        self.assertTrue(all("trust_score" in item["score_breakdown"]
                            for item in result.traces[0]["assignments"]))
        self.assertTrue(all("trust_updates" in stats for stats in result.agent_stats))

    def test_trust_aware_requires_adaptive_scheduler(self):
        with self.assertRaises(ValueError):
            mc.ExperimentRunner(
                self.questions[:1], use_chain=False,
                scheduling="static", trust_aware=True
            )

    def test_latency_metrics_are_same_unit(self):
        """三组耗时必须同口径（LLM 耗时 + 框架开销），否则无法横向比较。"""
        for group in self.results.values():
            for rec in group.records:
                self.assertGreater(rec.llm_latency_ms, 0)
                self.assertGreaterEqual(rec.framework_latency_ms, 0)
                self.assertAlmostEqual(
                    rec.latency_ms, rec.llm_latency_ms + rec.framework_latency_ms, places=4
                )

    def test_summary_is_json_serializable(self):
        for group in self.results.values():
            json.dumps(group.summary())

    def test_agent_task_counts_match_subtask_total(self):
        for group_key in ("b", "c"):
            group = self.results[group_key]
            dispatched = sum(rec.subtask_count for rec in group.records)
            handled = sum(group.agent_loads.values())
            self.assertEqual(dispatched, handled, f"{group_key} 组派发与承接数量不符")


class TestReproducibility(unittest.TestCase):
    """同种子必须复现，否则对照实验的结论站不住。"""

    def test_same_seed_same_results(self):
        questions = mc.load_dataset()[:6]

        def run():
            runner = mc.ExperimentRunner(questions, agent_count=4, seed=42, use_chain=False)
            res = runner.run(["a", "b", "c"])
            return {k: (v.accuracy, v.hallucinations) for k, v in res.items()}

        self.assertEqual(run(), run())

    def test_different_seed_may_differ(self):
        questions = mc.load_dataset()[:12]

        def run(seed):
            runner = mc.ExperimentRunner(questions, agent_count=4, seed=seed, use_chain=False)
            return runner.run(["a"])["a"]

        # 不同种子下 A 组的具体答案序列应当不同（幻觉落点不同）
        a1 = [r.final_answer for r in run(1).records]
        a2 = [r.final_answer for r in run(98765).records]
        self.assertNotEqual(a1, a2)


class TestExport(unittest.TestCase):
    """产出文件的完整性。"""

    @classmethod
    def setUpClass(cls):
        questions = mc.load_dataset()[:5]
        runner = mc.ExperimentRunner(questions, agent_count=3, seed=7, use_chain=False)
        cls.results = runner.run(["a", "b", "c"])

    def test_csv_and_jsonl_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            written = mc.export_results(self.results, out)
            for key in ("metrics", "per_question", "agent_load", "trace"):
                self.assertTrue(written[key].exists(), f"{key} 未生成")
                self.assertGreater(written[key].stat().st_size, 0)

    def test_metrics_csv_has_three_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = mc.export_results(self.results, Path(tmp))["metrics"]
            with path.open(encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 3)
            self.assertEqual({r["group"] for r in rows}, {"a", "b", "c"})

    def test_trace_jsonl_lines_are_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = mc.export_results(self.results, Path(tmp))["trace"]
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                self.assertIn("group", record)
                self.assertIn("question_id", record)

    def test_plots_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            figures = mc.plot_results(self.results, Path(tmp))
            self.assertEqual(len(figures), 3)
            for path in figures:
                self.assertTrue(path.exists())
                self.assertGreater(path.stat().st_size, 1000)  # 不是空图


class TestCli(unittest.TestCase):
    def test_parse_modes(self):
        self.assertEqual(mc.parse_modes("all"), ["a", "b", "c"])
        self.assertEqual(mc.parse_modes("c"), ["c"])
        self.assertEqual(mc.parse_modes("bc"), ["b", "c"])

    def test_invalid_mode_exits(self):
        with self.assertRaises(SystemExit):
            mc.parse_modes("xyz")

    def test_trust_aware_cli_flag(self):
        args = mc.build_parser().parse_args(
            ["--mode", "c", "--scheduling", "adaptive", "--trust-aware"]
        )
        self.assertTrue(args.trust_aware)

    def test_main_end_to_end(self):
        """完整跑一遍 CLI，确认不崩且产出齐全。"""
        with tempfile.TemporaryDirectory() as tmp:
            code = mc.main(
                ["--mode", "all", "--no-chain", "--limit", "4", "--out", tmp, "--no-plot"]
            )
            self.assertEqual(code, 0)
            self.assertTrue((Path(tmp) / "metrics.csv").exists())


class TestRedundantVerification(unittest.TestCase):
    """D3/D4：预算内冗余验证在实验层的接入、方法口径与成本统计。"""

    def _runner(self, **kwargs) -> mc.ExperimentRunner:
        questions = mc.load_dataset()[:4]
        return mc.ExperimentRunner(
            questions, agent_count=4, seed=20250728, use_chain=False, **kwargs
        )

    def test_baseline_method_forces_zero_budget(self):
        """B0 必须强制关掉冗余，保证与既有 C 组口径完全一致。"""
        runner = self._runner(redundancy_budget=8, method="baseline")
        self.assertEqual(runner.redundancy_budget, 0)

    def test_method_selects_scheduling_and_aggregation(self):
        proposed = self._runner(redundancy_budget=4, method="proposed")
        self.assertTrue(proposed.correlation_aware)
        self.assertEqual(proposed.redundancy_strategy, "greedy")

        reputation = self._runner(redundancy_budget=4, method="reputation")
        self.assertFalse(reputation.correlation_aware)
        self.assertEqual(reputation.redundancy_strategy, "greedy")

        randomMethod = self._runner(redundancy_budget=4, method="random")
        self.assertEqual(randomMethod.redundancy_strategy, "random")

    def test_error_group_modes(self):
        self.assertIsNone(self._runner(correlation_level="none").resolve_error_groups())
        self.assertEqual(
            self._runner(correlation_level="independent").resolve_error_groups(),
            ["g0", "g1", "g2", "g3"],
        )
        self.assertEqual(
            self._runner(correlation_level="pairs").resolve_error_groups(),
            ["g0", "g0", "g1", "g1"],
        )
        self.assertEqual(
            self._runner(correlation_level="all").resolve_error_groups(), ["g0"] * 4
        )

    def test_profiles_align_with_error_groups(self):
        """分组必须与画像对齐：同画像 → 同组，冷启动先验才预测得到相关性。"""
        self.assertIsNone(self._runner(correlation_level="none").resolve_profiles())

        independent = self._runner(correlation_level="independent").resolve_profiles()
        self.assertEqual(len(independent), 4)
        self.assertEqual(len({tuple(sorted(p.items())) for p in independent}), 4)

        pairs = self._runner(correlation_level="pairs").resolve_profiles()
        self.assertEqual(pairs[0], pairs[1])
        self.assertEqual(pairs[2], pairs[3])
        self.assertNotEqual(pairs[0], pairs[2])

        everyone = self._runner(correlation_level="all").resolve_profiles()
        self.assertEqual(len({tuple(sorted(p.items())) for p in everyone}), 1)

    def test_invalid_error_group_mode_raises(self):
        with self.assertRaises(ValueError):
            self._runner(correlation_level="bogus").resolve_error_groups()

    def test_baseline_matches_run_without_new_options(self):
        """回归护栏：不传新参数 与 method=baseline 必须给出同一结果。"""
        plain = self._runner().run(["c"])["c"]
        baseline = self._runner(method="baseline", correlation_level="none").run(["c"])["c"]
        self.assertEqual(plain.summary()["accuracy"], baseline.summary()["accuracy"])
        self.assertEqual(plain.summary()["hallucinations"], baseline.summary()["hallucinations"])
        self.assertEqual(baseline.redundant_calls_total, 0)

    def test_proposed_spends_budget_and_records_cost(self):
        result = self._runner(redundancy_budget=3, method="proposed").run(["c"])["c"]
        self.assertGreater(result.redundant_calls_total, 0)
        self.assertEqual(result.redundancy_budget, 3)
        self.assertTrue(result.correlation_aware)

    def test_error_group_run_is_reproducible(self):
        first = self._runner(redundancy_budget=2, correlation_level="all").run(["c"])["c"]
        second = self._runner(redundancy_budget=2, correlation_level="all").run(["c"])["c"]
        self.assertEqual(
            [r.final_answer for r in first.records],
            [r.final_answer for r in second.records],
        )

    def test_error_acceptance_rate_is_complement_of_accuracy(self):
        result = self._runner().run(["c"])["c"]
        self.assertAlmostEqual(
            result.error_acceptance_rate, 1.0 - result.accuracy, places=6
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
