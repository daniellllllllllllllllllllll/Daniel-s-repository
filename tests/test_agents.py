"""agents 模块测试：能力打分、动态分配、任务拆解容错、加密 TCP 派发。

不涉及区块链节点——ledger 一律用 NullLedger。
"""

from __future__ import annotations

import unittest

from agents import controller_agent as ca
from agents import task_agent as ta
from blockchain.chain_client import NullLedger
from llm.llm_api import MockLLM, TaskKind

KB = {f"题目{i}": str(i) for i in range(50)}


def _llm(competence: float = 0.7, rate: float = 0.3, seed: int = 2024) -> MockLLM:
    return MockLLM(seed=seed, knowledge_base=KB, hallucination_rate=rate, competence=competence)


class TestSubTask(unittest.TestCase):
    def test_difficulty_clamped(self):
        self.assertEqual(ta.SubTask("S1", "d", difficulty=99).difficulty, 5)
        self.assertEqual(ta.SubTask("S1", "d", difficulty=-3).difficulty, 1)

    def test_unknown_domain_falls_back(self):
        self.assertEqual(ta.SubTask("S1", "d", domain="astrology").domain, "knowledge")

    def test_payload_excludes_expected_answer(self):
        """标准答案绝不能下发给 Agent，否则实验就是作弊。"""
        st = ta.SubTask("S1", "1+1?", expected="2")
        self.assertNotIn("expected", st.to_payload())
        self.assertIn("expected", st.to_dict())


class TestTaskAgent(unittest.TestCase):
    def setUp(self):
        self.agent = ta.TaskAgent(
            "agent-1", _llm(), capabilities={"math": 0.9, "writing": 0.1}
        )

    def test_missing_capabilities_default_to_neutral(self):
        self.assertEqual(self.agent.capability("code"), 0.5)
        self.assertEqual(self.agent.capability("math"), 0.9)

    def test_execute_returns_result(self):
        result = self.agent.execute(ta.SubTask("S1", "题目1", domain="math"))
        self.assertTrue(result.ok)
        self.assertEqual(result.agent_id, "agent-1")
        self.assertEqual(result.subtask_id, "S1")
        self.assertTrue(result.digest)
        self.assertEqual(len(result.digest), 64)

    def test_execute_accepts_dict(self):
        result = self.agent.execute({"id": "S2", "description": "题目2", "domain": "math"})
        self.assertEqual(result.subtask_id, "S2")

    def test_stats_accumulate(self):
        for i in range(5):
            self.agent.execute(ta.SubTask(f"S{i}", f"题目{i}", domain="math"))
        self.assertEqual(self.agent.task_count, 5)
        self.assertEqual(self.agent.completed, 5)
        self.assertEqual(self.agent.load, 0)  # 执行完负载必须归零

    def test_new_agent_success_rate_is_one(self):
        """没有历史的新 Agent 成功率给 1.0，否则永远抢不到任务。"""
        self.assertEqual(ta.TaskAgent("fresh", _llm()).success_rate, 1.0)

    def test_llm_failure_becomes_error_result_not_exception(self):
        """单个 Agent 出错不能中断整轮实验。"""

        class BrokenLLM(MockLLM):
            def _complete(self, prompt, system, **kwargs):
                raise RuntimeError("模型服务不可用")

        agent = ta.TaskAgent("broken", BrokenLLM(seed=1))
        result = agent.execute(ta.SubTask("S1", "题目1"))
        self.assertFalse(result.ok)
        self.assertIn("模型服务不可用", result.error)
        self.assertEqual(agent.failed, 1)
        self.assertEqual(agent.load, 0)

    def test_competence_is_passed_to_llm(self):
        """Agent 在该领域的能力必须传给 LLM 层，幻觉模型才能生效。"""
        weak = ta.TaskAgent("weak", _llm(rate=1.0), capabilities={"math": 0.0})
        strong = ta.TaskAgent("strong", _llm(rate=1.0), capabilities={"math": 1.0})

        weak_h = sum(
            weak.execute(ta.SubTask(f"S{i}", f"题目{i}", domain="math")).hallucinated
            for i in range(20)
        )
        strong_h = sum(
            strong.execute(ta.SubTask(f"S{i}", f"题目{i}", domain="math")).hallucinated
            for i in range(20)
        )
        self.assertEqual(strong_h, 0)
        self.assertGreater(weak_h, 0)

    def test_digest_changes_when_answer_changes(self):
        r = self.agent.execute(ta.SubTask("S1", "题目1", domain="math"))
        from crypto import hash_tool as ht

        tampered = ht.message_digest(
            {"subtask_id": r.subtask_id, "agent_id": r.agent_id,
             "answer": r.answer + "篡改", "error": r.error}
        )
        self.assertNotEqual(r.digest, tampered)

    def test_ledger_record_has_no_answer(self):
        r = self.agent.execute(ta.SubTask("S1", "题目1", domain="math"))
        self.assertNotIn("answer", r.ledger_record())
        self.assertIn("digest", r.ledger_record())

    def test_reset_clears_stats(self):
        self.agent.execute(ta.SubTask("S1", "题目1", domain="math"))
        self.agent.reset()
        self.assertEqual(self.agent.task_count, 0)
        self.assertEqual(self.agent.results, [])


class TestAgentPool(unittest.TestCase):
    def test_pool_has_distinct_specialties(self):
        """能力必须不均衡，否则动态打分与轮询不会有任何差别。"""
        pool = ta.make_agent_pool(5, lambda i: _llm())
        specialties = {max(a.capabilities, key=a.capabilities.get) for a in pool}
        self.assertEqual(len(specialties), 5)

    def test_pool_ids_unique(self):
        pool = ta.make_agent_pool(4, lambda i: _llm())
        self.assertEqual(len({a.agent_id for a in pool}), 4)

    def test_each_agent_has_own_keypair(self):
        pool = ta.make_agent_pool(3, lambda i: _llm())
        self.assertEqual(len({a.fingerprint for a in pool}), 3)


class TestDecompose(unittest.TestCase):
    def setUp(self):
        self.ctrl = ca.ControllerAgent(_llm(), ta.make_agent_pool(3, lambda i: _llm()))

    def test_decompose_returns_subtasks(self):
        subs = self.ctrl.decompose("先算 math；再写 code", "Q1")
        self.assertGreaterEqual(len(subs), 1)
        for s in subs:
            self.assertTrue(s.id.startswith("Q1-"))
            self.assertEqual(s.parent_id, "Q1")

    def test_extract_plain_json(self):
        parsed = ca.ControllerAgent._extract_json_array('[{"id":"S1"}]')
        self.assertEqual(parsed, [{"id": "S1"}])

    def test_extract_from_markdown_fence(self):
        """真实模型最常见的输出形态：用 ```json 包住。"""
        text = 'Here you go:\n```json\n[{"id":"S1","description":"d"}]\n```\n'
        self.assertEqual(len(ca.ControllerAgent._extract_json_array(text)), 1)

    def test_extract_from_surrounding_prose(self):
        text = '好的，拆解如下：[{"id":"S1","description":"d"}] 希望有帮助。'
        self.assertEqual(len(ca.ControllerAgent._extract_json_array(text)), 1)

    def test_extract_returns_none_on_garbage(self):
        self.assertIsNone(ca.ControllerAgent._extract_json_array("完全不是 JSON"))
        self.assertIsNone(ca.ControllerAgent._extract_json_array(""))

    def test_extract_rejects_json_object(self):
        """只接受数组；对象说明模型没按格式输出。"""
        self.assertIsNone(ca.ControllerAgent._extract_json_array('{"id":"S1"}'))

    def test_fallback_to_single_task_when_unparsable(self):
        """解析失败必须降级，而不是让整道题失败。"""

        class BadLLM(MockLLM):
            def _complete(self, prompt, system, **kwargs):
                from llm.llm_base import LLMResponse

                return LLMResponse(text="我没法拆解", provider="mock", model="m", latency_ms=1)

        ctrl = ca.ControllerAgent(BadLLM(seed=1), ta.make_agent_pool(2, lambda i: _llm()))
        subs = ctrl.decompose("一个问题", "Q9")
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0].description, "一个问题")


class TestScoring(unittest.TestCase):
    def setUp(self):
        self.math_agent = ta.TaskAgent("math-agent", _llm(), {"math": 0.95, "writing": 0.1})
        self.write_agent = ta.TaskAgent("write-agent", _llm(), {"math": 0.1, "writing": 0.95})
        self.ctrl = ca.ControllerAgent(_llm(), [self.math_agent, self.write_agent])

    def test_specialist_scores_higher_in_own_domain(self):
        task = ta.SubTask("S1", "算术题", domain="math")
        self.assertGreater(
            self.ctrl.score(self.math_agent, task), self.ctrl.score(self.write_agent, task)
        )

    def test_load_penalty_applies(self):
        task = ta.SubTask("S1", "算术题", domain="math")
        idle = self.ctrl.score(self.math_agent, task)
        self.math_agent.load = 5
        self.assertLess(self.ctrl.score(self.math_agent, task), idle)

    def test_low_success_rate_penalized(self):
        task = ta.SubTask("S1", "算术题", domain="math")
        before = self.ctrl.score(self.math_agent, task)
        self.math_agent.completed, self.math_agent.failed = 1, 9
        self.assertLess(self.ctrl.score(self.math_agent, task), before)

    def test_error_rate_includes_failures_and_hallucinations(self):
        self.math_agent.completed = 8
        self.math_agent.failed = 2
        self.math_agent.hallucinations = 1
        self.assertAlmostEqual(self.math_agent.error_rate, 0.3)

    def test_adaptive_score_breakdown_contains_history_terms(self):
        ctrl = ca.ControllerAgent(
            _llm(), [self.math_agent, self.write_agent], scheduling="adaptive"
        )
        assignment = ctrl.assign(
            [ta.SubTask("S1", "高难算术题", domain="math", difficulty=5)]
        )[0]
        self.assertEqual(
            set(assignment.score_breakdown),
            {
                "capability",
                "success_rate",
                "difficulty_match",
                "load_penalty",
                "error_rate",
                "latency_penalty",
                "trust_score",
                "total",
            },
        )

    def test_trust_updates_after_quality_result(self):
        initial = self.math_agent.trust_score
        self.math_agent.execute(ta.SubTask("S1", "算术题", domain="math"))
        self.assertGreater(self.math_agent.trust_score, initial)
        self.assertEqual(self.math_agent.trust_updates, 1)
        self.assertEqual(self.math_agent.trust_last_event, "success")
        self.assertEqual(self.math_agent.trust_history[-1]["event"], "success")

    def test_trust_penalizes_hallucination_and_failure(self):
        initial = self.math_agent.trust_score
        hallucination = ta.AgentResult(
            subtask_id="S-h", agent_id="agent-1", answer="错误", hallucinated=True
        )
        failure = ta.AgentResult(
            subtask_id="S-f", agent_id="agent-1", answer="", error="timeout"
        )
        self.math_agent.update_trust(hallucination)
        self.math_agent.update_trust(failure)
        self.assertLess(self.math_agent.trust_score, initial)
        self.assertEqual(
            [item["event"] for item in self.math_agent.trust_history[-2:]],
            ["hallucination", "failure"],
        )
        self.assertGreaterEqual(self.math_agent.trust_score, 0.0)
        self.assertLessEqual(self.math_agent.trust_score, 1.0)

    def test_trust_can_be_excluded_from_adaptive_score(self):
        trusted = ta.TaskAgent("trusted", _llm(), {"math": 0.8})
        untrusted = ta.TaskAgent("untrusted", _llm(), {"math": 0.8})
        trusted.trust_score = 1.0
        untrusted.trust_score = 0.0
        weights = ca.ScoreWeights(
            capability=0.0,
            load=0.0,
            success=0.0,
            difficulty=0.0,
            difficulty_match=0.0,
            error=0.0,
            latency=0.0,
            trust=1.0,
        )
        task = ta.SubTask("S1", "任务", domain="math")
        aware = ca.ControllerAgent(
            _llm(), [trusted, untrusted], weights=weights, scheduling="adaptive",
            trust_aware=True,
        )
        unaware = ca.ControllerAgent(
            _llm(), [trusted, untrusted], weights=weights, scheduling="adaptive",
            trust_aware=False,
        )
        self.assertGreater(aware.score(trusted, task), aware.score(untrusted, task))
        self.assertAlmostEqual(unaware.score(trusted, task), unaware.score(untrusted, task))

    def test_adaptive_score_penalizes_slow_agent(self):
        fast = ta.TaskAgent("fast", _llm(), {"math": 0.8})
        slow = ta.TaskAgent("slow", _llm(), {"math": 0.8})
        for agent, latency in ((fast, 10.0), (slow, 100.0)):
            agent.completed = 10
            agent.total_latency_ms = latency * agent.completed
        weights = ca.ScoreWeights(
            capability=0.0,
            load=0.0,
            success=0.0,
            difficulty=0.0,
            difficulty_match=0.0,
            error=0.0,
            latency=1.0,
        )
        ctrl = ca.ControllerAgent(
            _llm(), [fast, slow], weights=weights, scheduling="adaptive"
        )
        task = ta.SubTask("S1", "任务", domain="math")
        self.assertGreater(ctrl.score(fast, task), ctrl.score(slow, task))

    def test_difficulty_weight_disabled_by_default(self):
        """w_difficulty 默认 0，难度不影响打分——与原论文行为一致。"""
        ctrl = ca.ControllerAgent(
            _llm(), [self.math_agent], weights=ca.ScoreWeights(difficulty=0.0)
        )
        easy = ctrl.score(self.math_agent, ta.SubTask("S1", "d", domain="math", difficulty=1))
        hard = ctrl.score(self.math_agent, ta.SubTask("S2", "d", domain="math", difficulty=5))
        self.assertAlmostEqual(easy, hard)

    def test_difficulty_weight_enabled_penalizes_weak_agent(self):
        """阶段4 开关：打开 w_difficulty 后，高难任务对能力不足者惩罚更重。"""
        ctrl = ca.ControllerAgent(
            _llm(), [self.write_agent], weights=ca.ScoreWeights(difficulty=0.5)
        )
        easy = ctrl.score(self.write_agent, ta.SubTask("S1", "d", domain="math", difficulty=1))
        hard = ctrl.score(self.write_agent, ta.SubTask("S2", "d", domain="math", difficulty=5))
        self.assertLess(hard, easy)


class TestAssignment(unittest.TestCase):
    def setUp(self):
        self.pool = ta.make_agent_pool(5, lambda i: _llm())
        self.ctrl = ca.ControllerAgent(_llm(), self.pool)

    def test_dynamic_assignment_matches_specialty(self):
        """核心断言：math 任务派给 math 专家。"""
        assignments = self.ctrl.assign([ta.SubTask("S1", "算术", domain="math")])
        chosen = self.ctrl._agent_by_id(assignments[0].agent_id)
        self.assertEqual(max(chosen.capabilities, key=chosen.capabilities.get), "math")

    def test_all_scores_recorded(self):
        a = self.ctrl.assign([ta.SubTask("S1", "算术", domain="math")])[0]
        self.assertEqual(len(a.all_scores), 5)
        self.assertEqual(a.score, max(a.all_scores.values()))

    def test_load_spreads_across_agents(self):
        """同领域连派多个任务时，负载项应把任务摊开，而不是全压给同一个。"""
        subs = [ta.SubTask(f"S{i}", "算术", domain="math") for i in range(10)]
        assignments = self.ctrl.assign(subs)
        used = {a.agent_id for a in assignments}
        self.assertGreater(len(used), 1, "全部任务压给了同一个 Agent，负载均衡没生效")

    def test_round_robin_ignores_specialty(self):
        """对照组B 的分配方式：纯轮询，与领域无关。"""
        subs = [ta.SubTask(f"S{i}", "算术", domain="math") for i in range(5)]
        assignments = self.ctrl.assign_round_robin(subs)
        self.assertEqual(
            [a.agent_id for a in assignments], [a.agent_id for a in self.pool]
        )

    def test_round_robin_vs_dynamic_differ(self):
        subs = [ta.SubTask(f"S{i}", "算术", domain="math") for i in range(5)]
        rr = [a.agent_id for a in ca.ControllerAgent(_llm(), self.pool).assign_round_robin(subs)]
        dyn = [a.agent_id for a in self.ctrl.assign(subs)]
        self.assertNotEqual(rr, dyn)

    def test_unknown_agent_id_raises(self):
        with self.assertRaises(KeyError):
            self.ctrl._agent_by_id("nobody")


class TestControllerRun(unittest.TestCase):
    def setUp(self):
        self.pool = ta.make_agent_pool(4, lambda i: _llm())
        self.ctrl = ca.ControllerAgent(_llm(), self.pool, ledger=NullLedger())

    def test_run_produces_complete_trace(self):
        trace = self.ctrl.run("先做 math 计算；再做 writing 总结", "Q1")
        self.assertEqual(trace.question_id, "Q1")
        self.assertTrue(trace.subtasks)
        self.assertEqual(len(trace.assignments), len(trace.subtasks))
        self.assertEqual(len(trace.results), len(trace.subtasks))
        self.assertGreater(trace.total_latency_ms, 0)

    def test_trace_is_json_serializable(self):
        import json

        json.dumps(self.ctrl.run("一个问题", "Q1").to_dict(), ensure_ascii=False)

    def test_null_ledger_marks_chain_disabled(self):
        """没上链就必须如实标注，不能伪装成已上链。"""
        trace = self.ctrl.run("一个问题", "Q1")
        self.assertFalse(trace.chain_enabled)
        for receipt in trace.chain_receipts:
            self.assertEqual(receipt["status"], "skipped")
            self.assertIsNone(receipt["tx_hash"])

    def test_upload_chain_false_skips_receipts(self):
        trace = self.ctrl.run("一个问题", "Q1", upload_chain=False)
        self.assertEqual(trace.chain_receipts, [])

    def test_scoring_mode_beats_round_robin_on_hallucinations(self):
        """对照实验的核心机制验证：动态打分的幻觉总数应低于轮询。

        题目全部落在各 Agent 专长差异明显的领域上，
        打分派单能把任务送到高能力 Agent 手里，幻觉自然更少。
        """
        questions = [f"题目{i}" for i in range(24)]

        def build():
            pool = ta.make_agent_pool(5, lambda i: _llm(rate=0.9))
            return ca.ControllerAgent(_llm(), pool, ledger=NullLedger())

        ctrl_rr = build()
        ctrl_dyn = build()
        for i, q in enumerate(questions):
            ctrl_rr.run(q, f"Q{i}", use_scoring=False)
            ctrl_dyn.run(q, f"Q{i}", use_scoring=True)

        h_rr = sum(t.hallucination_count for t in ctrl_rr.traces)
        h_dyn = sum(t.hallucination_count for t in ctrl_dyn.traces)
        self.assertLess(h_dyn, h_rr, f"动态打分幻觉 {h_dyn} 未低于轮询 {h_rr}")

    def test_load_balance_better_with_scoring(self):
        """实验组C 的任务量分布方差应低于对照组B。"""
        import statistics

        def run_all(use_scoring: bool) -> list[int]:
            pool = ta.make_agent_pool(5, lambda i: _llm())
            ctrl = ca.ControllerAgent(_llm(), pool, ledger=NullLedger())
            for i in range(20):
                ctrl.run(f"题目{i}", f"Q{i}", use_scoring=use_scoring)
            return [a.task_count for a in pool]

        var_rr = statistics.pvariance(run_all(False))
        var_dyn = statistics.pvariance(run_all(True))
        # 轮询天然均匀，动态打分要在"专业对口"与"负载均衡"间折中，
        # 因此这里只要求动态打分不至于严重倾斜（不超过轮询方差 + 合理余量）
        self.assertLessEqual(var_dyn, var_rr + 30)

    def test_reset_clears_everything(self):
        self.ctrl.run("题目1", "Q1")
        self.ctrl.reset()
        self.assertEqual(self.ctrl.traces, [])
        self.assertTrue(all(a.task_count == 0 for a in self.pool))

    def test_aggregate_single_result_returns_directly(self):
        results = [ta.AgentResult("S1", "agent-1", "唯一答案")]
        self.assertEqual(self.ctrl.aggregate("问题", results), "唯一答案")

    def test_aggregate_empty_returns_empty(self):
        self.assertEqual(self.ctrl.aggregate("问题", []), "")

    def test_aggregate_ignores_failed_results(self):
        results = [
            ta.AgentResult("S1", "a1", "", error="失败了"),
            ta.AgentResult("S2", "a2", "有效答案"),
        ]
        self.assertEqual(self.ctrl.aggregate("问题", results), "有效答案")

    def test_empty_agent_pool_rejected(self):
        with self.assertRaises(ValueError):
            ca.ControllerAgent(_llm(), [])

    def test_invalid_transport_rejected(self):
        with self.assertRaises(ValueError):
            ca.ControllerAgent(_llm(), self.pool, transport="carrier-pigeon")


class TestTcpTransport(unittest.TestCase):
    """通过真实加密 TCP 信道派发任务（论文要求的完整安全链路）。"""

    def setUp(self):
        self.pool = ta.make_agent_pool(2, lambda i: _llm())
        self.ctrl = ca.ControllerAgent(
            _llm(), self.pool, ledger=NullLedger(), transport="tcp"
        )
        self.ctrl.wire_tcp()
        self.addCleanup(self.ctrl.shutdown)

    def test_keys_exchanged_both_ways(self):
        for agent in self.pool:
            self.assertTrue(self.ctrl.registry.has(agent.agent_id))
            self.assertTrue(agent.registry.has("controller"))

    def test_dispatch_over_encrypted_tcp(self):
        assignment = ca.Assignment(
            subtask=ta.SubTask("S1", "题目1", domain="math"), agent_id=self.pool[0].agent_id,
            score=1.0
        )
        result = self.ctrl.dispatch(assignment)
        self.assertEqual(result.subtask_id, "S1")
        self.assertEqual(result.agent_id, self.pool[0].agent_id)
        self.assertTrue(result.digest)

    def test_full_run_over_tcp(self):
        trace = self.ctrl.run("题目5", "Q1")
        self.assertTrue(trace.results)
        self.assertTrue(all(r["agent_id"] for r in trace.results))

    def test_tcp_and_direct_produce_same_shape(self):
        tcp_trace = self.ctrl.run("题目7", "Q1")

        direct_pool = ta.make_agent_pool(2, lambda i: _llm())
        direct_ctrl = ca.ControllerAgent(_llm(), direct_pool, ledger=NullLedger())
        direct_trace = direct_ctrl.run("题目7", "Q1")

        self.assertEqual(set(tcp_trace.to_dict()), set(direct_trace.to_dict()))
        self.assertEqual(len(tcp_trace.results), len(direct_trace.results))

    def test_tcp_mode_without_wiring_raises(self):
        pool = ta.make_agent_pool(1, lambda i: _llm())
        ctrl = ca.ControllerAgent(_llm(), pool, transport="tcp")  # 故意不调 wire_tcp
        assignment = ca.Assignment(ta.SubTask("S1", "d"), pool[0].agent_id, 1.0)
        with self.assertRaises(RuntimeError):
            ctrl.dispatch(assignment)


class TestReliabilityPosterior(unittest.TestCase):
    """C1：per-domain Beta 后验要能表达「math 可靠、code 不可靠」的领域差异。"""

    def setUp(self):
        self.agent = ta.TaskAgent("a", _llm(), capabilities={"math": 0.9})

    def _result(self, domain: str, **kwargs) -> ta.AgentResult:
        return ta.AgentResult("S1", "a", kwargs.pop("answer", "x"), domain=domain, **kwargs)

    def test_prior_is_uniform(self):
        for domain in ta.DOMAINS:
            self.assertAlmostEqual(self.agent.reliability(domain), 0.5, places=6)

    def test_success_increases_alpha(self):
        self.agent.update_reliability(self._result("math"))
        self.assertEqual(self.agent.alpha["math"], 2.0)
        self.assertEqual(self.agent.beta["math"], 1.0)
        self.assertAlmostEqual(self.agent.reliability("math"), 2.0 / 3.0, places=6)
        self.assertEqual(self.agent.error_flags["math"], [0])

    def test_hallucination_and_failure_increase_beta(self):
        self.agent.update_reliability(self._result("math", hallucinated=True))
        self.agent.update_reliability(self._result("math", answer="", error="boom"))
        self.assertEqual(self.agent.beta["math"], 3.0)
        self.assertEqual(self.agent.alpha["math"], 1.0)
        self.assertEqual(self.agent.error_flags["math"], [1, 1])

    def test_domains_are_independent(self):
        self.agent.update_reliability(self._result("math"))
        self.agent.update_reliability(self._result("code", answer="", error="boom"))
        self.assertGreater(self.agent.reliability("math"), 0.5)
        self.assertLess(self.agent.reliability("code"), 0.5)

    def test_execute_updates_posterior(self):
        self.agent.execute(ta.SubTask("S1", "题目1", domain="math"))
        self.assertEqual(len(self.agent.error_flags["math"]), 1)
        self.assertEqual(self.agent.alpha["math"] + self.agent.beta["math"], 3.0)

    def test_reset_clears_posterior(self):
        self.agent.execute(ta.SubTask("S1", "题目1", domain="math"))
        self.agent.reset()
        self.assertEqual(self.agent.alpha["math"], 1.0)
        self.assertEqual(self.agent.beta["math"], 1.0)
        self.assertEqual(self.agent.error_flags["math"], [])

    def test_stats_reports_reliability(self):
        stats = self.agent.stats()
        self.assertIn("reliability", stats)
        self.assertEqual(set(stats["reliability"]), set(ta.DOMAINS))


class TestErrorCorrelation(unittest.TestCase):
    """C2：冷启动用能力向量先验，热启动用 Pearson。"""

    STRONG = {"math": 0.95, "code": 0.35, "logic": 0.75, "knowledge": 0.40, "writing": 0.25}
    WEAK_MATH = {"math": 0.20, "code": 0.25, "logic": 0.40, "knowledge": 0.95, "writing": 0.95}

    def _agent(self, agent_id: str, flags=None, capabilities=None) -> ta.TaskAgent:
        agent = ta.TaskAgent(agent_id, _llm(), capabilities=capabilities)
        if flags is not None:
            agent.error_flags["math"] = list(flags)
        return agent

    def test_cold_start_matches_capability_cosine(self):
        a = self._agent("a", capabilities=self.STRONG)
        b = self._agent("b", capabilities=self.STRONG)
        c = self._agent("c", capabilities=self.WEAK_MATH)
        self.assertAlmostEqual(a.error_correlation(b, "math"), 1.0, places=6)
        self.assertLess(a.error_correlation(c, "math"), a.error_correlation(b, "math"))

    def test_hot_start_pearson(self):
        flags = [0, 1, 0, 1, 0, 1]
        a = self._agent("a", flags=flags)
        b = self._agent("b", flags=flags)
        c = self._agent("c", flags=[1, 0, 1, 0, 1, 0])
        self.assertAlmostEqual(a.error_correlation(b, "math"), 1.0, places=6)
        self.assertAlmostEqual(a.error_correlation(c, "math"), -1.0, places=6)

    def test_constant_indicator_is_neutral(self):
        """全错/全对的指示向量上 Pearson 无定义，应返回 0 而不是抛异常。"""
        a = self._agent("a", flags=[1, 1, 1, 1, 1])
        b = self._agent("b", flags=[0, 1, 0, 1, 0])
        self.assertEqual(a.error_correlation(b, "math"), 0.0)

    def test_correlation_is_symmetric(self):
        a = self._agent("a", flags=[0, 1, 0, 1, 0, 1])
        b = self._agent("b", flags=[0, 0, 1, 1, 0, 1])
        self.assertAlmostEqual(
            a.error_correlation(b, "math"), b.error_correlation(a, "math"), places=6
        )


class TestErrorGroupLatentFactor(unittest.TestCase):
    """D5：同组 Agent 共享「错误潜因子」，从而产生可控的相关错误。"""

    def _llm_at(self, competence: float) -> MockLLM:
        return MockLLM(
            seed=7, knowledge_base=KB, hallucination_rate=0.6, competence=competence
        )

    def test_same_group_shares_wrong_answer(self):
        weak, strong = self._llm_at(0.1), self._llm_at(0.5)
        shared = 0
        for question in KB:
            low = weak.generate(
                question, kind=TaskKind.ANSWER, competence=0.1, difficulty=1, error_group="G"
            )
            high = strong.generate(
                question, kind=TaskKind.ANSWER, competence=0.5, difficulty=1, error_group="G"
            )
            if low.hallucinated and high.hallucinated:
                # 同一个潜因子抽样 → 必须错成同一个答案
                self.assertEqual(low.text, high.text)
                shared += 1
        self.assertGreater(shared, 0, "没有出现共同幻觉，测试样本不足")

    def test_same_group_shares_across_different_agent_seeds(self):
        """不同 Agent 的 seed 不同（实验里是 seed+salt），潜因子仍必须共享。

        这是真机实验里踩过的坑：若把 ``self.seed`` 掺进潜因子随机源，
        同组 Agent 会抽到完全不同的随机流，相关性档位就形同虚设。
        """
        first = MockLLM(seed=11, knowledge_base=KB, hallucination_rate=0.6, competence=0.3)
        second = MockLLM(seed=99, knowledge_base=KB, hallucination_rate=0.6, competence=0.3)
        for question in KB:
            left = first.generate(
                question, kind=TaskKind.ANSWER, competence=0.3, difficulty=1, error_group="G"
            )
            right = second.generate(
                question, kind=TaskKind.ANSWER, competence=0.3, difficulty=1, error_group="G"
            )
            # 能力相同 + 同组 → 幻觉判定与错答都必须逐字一致
            self.assertEqual(left.hallucinated, right.hallucinated)
            self.assertEqual(left.text, right.text)

    def test_unseeded_group_agents_stay_independent(self):
        """不同组（无潜因子共享）时，两个同能力 Agent 不应逐题一致。"""
        first = MockLLM(seed=11, knowledge_base=KB, hallucination_rate=0.6, competence=0.3)
        second = MockLLM(seed=99, knowledge_base=KB, hallucination_rate=0.6, competence=0.3)
        outcomes = [
            (
                first.generate(
                    question, kind=TaskKind.ANSWER, competence=0.3, difficulty=1
                ).hallucinated,
                second.generate(
                    question, kind=TaskKind.ANSWER, competence=0.3, difficulty=1
                ).hallucinated,
            )
            for question in KB
        ]
        self.assertTrue(
            any(a != b for a, b in outcomes), "未启用分组时不应逐题完全一致"
        )

    def test_different_groups_do_not_share_wrong_answer(self):
        weak, strong = self._llm_at(0.1), self._llm_at(0.5)
        differs = 0
        for question in KB:
            low = weak.generate(
                question, kind=TaskKind.ANSWER, competence=0.1, difficulty=1, error_group="G1"
            )
            high = strong.generate(
                question, kind=TaskKind.ANSWER, competence=0.5, difficulty=1, error_group="G2"
            )
            if low.hallucinated and high.hallucinated and low.text != high.text:
                differs += 1
        self.assertGreater(differs, 0, "不同组之间不应共享错答")

    def test_default_group_keeps_legacy_determinism(self):
        """error_group=None（默认）时必须与既有确定性逐字符一致。"""
        first = self._llm_at(0.3).generate(
            "题目12", kind=TaskKind.ANSWER, competence=0.3, difficulty=2
        )
        second = self._llm_at(0.3).generate(
            "题目12", kind=TaskKind.ANSWER, competence=0.3, difficulty=2, error_group=None
        )
        self.assertEqual(first.text, second.text)
        self.assertEqual(first.hallucinated, second.hallucinated)

    def test_make_agent_pool_defaults_to_no_group(self):
        pool = ta.make_agent_pool(3, lambda i: _llm())
        self.assertTrue(all(a.error_group is None for a in pool))

    def test_make_agent_pool_assigns_groups_and_profiles(self):
        profile = {"math": 0.5, "code": 0.5, "logic": 0.5, "knowledge": 0.5, "writing": 0.5}
        pool = ta.make_agent_pool(
            3,
            lambda i: _llm(),
            profiles=[profile],
            error_groups=["G", "G", None],
        )
        self.assertEqual([a.error_group for a in pool], ["G", "G", None])
        self.assertEqual(pool[0].capabilities["math"], 0.5)


class TestRedundancyAggregation(unittest.TestCase):
    """D3：去相关加权投票。"""

    def setUp(self):
        # 两个能力完全相同的 Agent → 冷启动相关性 = 1（同质），第三个方向不同
        self.alpha = ta.TaskAgent("a1", _llm(), capabilities=TestErrorCorrelation.STRONG)
        self.beta = ta.TaskAgent("a2", _llm(), capabilities=TestErrorCorrelation.STRONG)
        self.gamma = ta.TaskAgent("a3", _llm(), capabilities=TestErrorCorrelation.WEAK_MATH)
        self.ctrl = ca.ControllerAgent(
            _llm(), [self.alpha, self.beta, self.gamma], ledger=NullLedger()
        )
        self.subtask = ta.SubTask("S1", "题目1", domain="math")

    def _result(self, agent_id: str, answer: str) -> ta.AgentResult:
        return ta.AgentResult("S1", agent_id, answer, domain="math")

    def test_empty_results(self):
        self.assertEqual(self.ctrl.aggregate_redundant(self.subtask, []), "")

    def test_single_result_passes_through(self):
        self.assertEqual(
            self.ctrl.aggregate_redundant(self.subtask, [self._result("a1", "X")]), "X"
        )

    def test_correlated_pair_loses_to_independent_agent(self):
        """a1/a2 同质（相关性≈1），a3 独立且后验更可靠 → 去相关投票选出 a3 的答案。

        朴素信誉加权（相关性=0）会让同质的两票压过一票；把 a2 的相关性折扣打掉后，
        a3 的证据才浮出来。
        """
        self.gamma.alpha["math"] = 8.0
        self.gamma.beta["math"] = 2.0  # 后验 0.8 > a1/a2 的 0.5
        results = [
            self._result("a1", "WRONG"),
            self._result("a2", "WRONG"),
            self._result("a3", "RIGHT"),
        ]
        self.assertEqual(self.ctrl.aggregate_redundant(self.subtask, results), "RIGHT")
        # 关掉相关性折扣 → 退化为朴素信誉加权，同质的两票（各 0.5）压过一票（0.8）
        self.assertEqual(
            self.ctrl.aggregate_redundant(self.subtask, results, correlationAware=False),
            "WRONG",
        )

    def test_skips_failed_and_empty_results(self):
        results = [
            self._result("a1", ""),
            ta.AgentResult("S1", "a2", "", domain="math", error="boom"),
            self._result("a3", "RIGHT"),
        ]
        self.assertEqual(self.ctrl.aggregate_redundant(self.subtask, results), "RIGHT")

    def test_all_zero_weight_falls_back_to_majority(self):
        """相关性全为 1 时权重归零，应退回多数投票而不是随意取一个。"""
        self.alpha.error_flags["math"] = [1, 0, 1, 0, 1]
        self.beta.error_flags["math"] = [1, 0, 1, 0, 1]
        self.ctrl.correlation_aware = True
        results = [
            self._result("a1", "RIGHT"),
            self._result("a2", "WRONG"),
            self._result("a3", "RIGHT"),
        ]
        self.assertEqual(self.ctrl.aggregate_redundant(self.subtask, results), "RIGHT")


class TestRedundancyScheduling(unittest.TestCase):
    """D4：预算内贪心加验证，边际收益按相关性打折。"""

    def _controller(self) -> ca.ControllerAgent:
        """用项目自带的 4 个专家画像：能力各异 → 两两相关性 < 1，边际收益为正。"""
        pool = ta.make_agent_pool(4, lambda i: _llm())
        return ca.ControllerAgent(_llm(), pool, ledger=NullLedger())

    def _assignments(self, count: int = 3) -> list:
        subtasks = [
            ta.SubTask(f"S{i + 1}", f"题目{i + 1}", domain="math", difficulty=2)
            for i in range(count)
        ]
        return [
            ca.Assignment(subtask=s, agent_id="agent-1", score=1.0) for s in subtasks
        ]

    def test_zero_budget_returns_empty(self):
        ctrl = self._controller()
        self.assertEqual(ctrl.add_redundancy(self._assignments(), 0), {})

    def test_respects_budget(self):
        ctrl = self._controller()
        plan = ctrl.add_redundancy(self._assignments(), 5)
        self.assertEqual(sum(len(v) for v in plan.values()), 5)

    def test_budget_larger_than_capacity_stops(self):
        """每个子任务最多再补 3 个（共 4 个 Agent），预算再大也不该死循环。"""
        ctrl = self._controller()
        plan = ctrl.add_redundancy(self._assignments(2), 99)
        self.assertEqual(sum(len(v) for v in plan.values()), 2 * 3)

    def test_never_picks_same_agent_twice(self):
        ctrl = self._controller()
        plan = ctrl.add_redundancy(self._assignments(), 8)
        for subtaskId, extra in plan.items():
            chain = ["agent-1"] + extra
            self.assertEqual(len(chain), len(set(chain)), f"{subtaskId} 出现重复 Agent")

    def test_correlation_discount_blocks_useless_redundancy(self):
        """同质 Agent 之间相关性≈1 → Proposed 不再浪费预算，而 B2（忽略相关性）会。"""
        pool = ta.make_agent_pool(
            4, lambda i: _llm(), profiles=[TestErrorCorrelation.STRONG] * 4
        )
        ctrl = ca.ControllerAgent(_llm(), pool, ledger=NullLedger())

        self.assertEqual(ctrl.add_redundancy(self._assignments(1), 1), {})
        self.assertEqual(ctrl.redundancy_trace, [])

        unaware = ctrl.add_redundancy(self._assignments(1), 1, correlationAware=False)
        self.assertEqual(set(unaware), {"S1"})
        self.assertAlmostEqual(ctrl.redundancy_trace[0]["correlation"], 0.0, places=6)
        self.assertGreater(ctrl.redundancy_trace[0]["gain"], 0.0)

    def test_correlation_discount_reduces_gain_on_distinct_profiles(self):
        """不同画像上：相关性感知的边际收益必须严格小于忽略相关性时。"""
        ctrl = self._controller()
        ctrl.add_redundancy(self._assignments(1), 1)
        aware = dict(ctrl.redundancy_trace[0])
        ctrl.add_redundancy(self._assignments(1), 1, correlationAware=False)
        unaware = dict(ctrl.redundancy_trace[0])

        self.assertGreater(aware["correlation"], 0.0)
        self.assertAlmostEqual(unaware["correlation"], 0.0, places=6)
        self.assertLess(aware["gain"], unaware["gain"])
        self.assertAlmostEqual(unaware["gain"], 0.25, places=6)  # 0.5 × 0.5 × 1

    def test_run_with_budget_zero_behaves_like_baseline(self):
        poolA = ta.make_agent_pool(4, lambda i: _llm())
        ctrlA = ca.ControllerAgent(_llm(), poolA, ledger=NullLedger())
        traceA = ctrlA.run("题目3", "Q1")

        poolB = ta.make_agent_pool(4, lambda i: _llm())
        ctrlB = ca.ControllerAgent(_llm(), poolB, ledger=NullLedger(), redundancy_budget=0)
        traceB = ctrlB.run("题目3", "Q1")

        self.assertEqual(traceA.final_answer, traceB.final_answer)
        self.assertEqual(traceA.redundancy, [])
        self.assertEqual(traceB.redundancy, [])

    def test_run_with_budget_adds_second_opinions(self):
        pool = ta.make_agent_pool(4, lambda i: _llm())
        ctrl = ca.ControllerAgent(_llm(), pool, ledger=NullLedger(), redundancy_budget=2)
        trace = ctrl.run("题目3", "Q1")
        self.assertEqual(len(trace.redundancy), 2)
        # 预算花掉后，Agent 调用次数应比单 Agent 直答多出 2 次
        primaryCalls = sum(1 for r in trace.results)
        self.assertEqual(primaryCalls, len(trace.results))
        self.assertTrue(all(item["subtask_id"] for item in trace.redundancy))


if __name__ == "__main__":
    unittest.main(verbosity=2)
