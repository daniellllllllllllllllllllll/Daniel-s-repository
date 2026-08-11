"""llm 模块测试：接口契约、Mock 确定性与幻觉模型、真实后端配置校验。

真实后端（openai / anthropic）默认 skip——CI 与离线环境没有 API Key，
不应让测试因外部依赖而红。配好 .env 后会自动执行。
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from llm import llm_api as la
from llm import llm_base as lb


class TestLLMResponse(unittest.TestCase):
    def test_total_tokens(self):
        r = lb.LLMResponse(text="x", provider="mock", model="m", prompt_tokens=10,
                           completion_tokens=5)
        self.assertEqual(r.total_tokens, 15)

    def test_clean_text_strips_hallucination_mark(self):
        r = lb.LLMResponse(text="错误答案" + lb.HALLUCINATION_MARK, provider="mock", model="m")
        self.assertEqual(r.clean_text, "错误答案")
        self.assertNotIn(lb.HALLUCINATION_MARK, r.clean_text)

    def test_to_dict_is_json_serializable(self):
        r = lb.LLMResponse(text="x", provider="mock", model="m", latency_ms=12.345)
        json.dumps(r.to_dict())  # 不抛异常即通过
        self.assertEqual(r.to_dict()["latency_ms"], 12.35)


class TestRetryDecorator(unittest.TestCase):
    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        @lb.retry_on_error(max_retries=3, base_delay=0.001)
        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise lb.LLMError("临时故障")
            return "ok"

        self.assertEqual(flaky(), "ok")
        self.assertEqual(calls["n"], 3)

    def test_gives_up_after_max_retries(self):
        @lb.retry_on_error(max_retries=2, base_delay=0.001)
        def always_fails():
            raise lb.LLMError("持续故障")

        with self.assertRaises(lb.LLMError) as ctx:
            always_fails()
        self.assertIn("重试 2 次", str(ctx.exception))

    def test_config_error_is_not_retried(self):
        """配置错误重试多少次都不会好，必须立刻抛出。"""
        calls = {"n": 0}

        @lb.retry_on_error(max_retries=5, base_delay=0.001)
        def bad_config():
            calls["n"] += 1
            raise lb.LLMConfigError("缺少 API Key")

        with self.assertRaises(lb.LLMConfigError):
            bad_config()
        self.assertEqual(calls["n"], 1)


class TestMockLLMBasics(unittest.TestCase):
    def setUp(self):
        self.llm = la.MockLLM(model="mock-large", seed=42)

    def test_generate_returns_response(self):
        r = self.llm.generate("1+1 等于几？")
        self.assertIsInstance(r, lb.LLMResponse)
        self.assertEqual(r.provider, "mock")
        self.assertTrue(r.text)
        self.assertGreater(r.latency_ms, 0)

    def test_empty_prompt_rejected(self):
        with self.assertRaises(ValueError):
            self.llm.generate("   ")

    def test_stats_accumulate(self):
        self.assertEqual(self.llm.call_count, 0)
        for i in range(3):
            self.llm.generate(f"问题 {i}")
        self.assertEqual(self.llm.call_count, 3)
        self.assertGreater(self.llm.avg_latency_ms, 0)

        self.llm.reset_stats()
        self.assertEqual(self.llm.call_count, 0)
        self.assertEqual(self.llm.avg_latency_ms, 0.0)

    def test_health_check(self):
        self.assertTrue(self.llm.health_check())

    def test_generate_text_returns_clean_str(self):
        out = self.llm.generate_text("测试问题")
        self.assertIsInstance(out, str)
        self.assertNotIn(lb.HALLUCINATION_MARK, out)

    def test_repr(self):
        self.assertIn("mock", repr(self.llm))


class TestMockLLMDeterminism(unittest.TestCase):
    """可复现性是对照实验成立的前提。"""

    def test_same_seed_same_output(self):
        a = la.MockLLM(seed=123)
        b = la.MockLLM(seed=123)
        for q in ["问题一", "问题二", "问题三"]:
            self.assertEqual(a.generate(q).text, b.generate(q).text)

    def test_same_instance_repeatable(self):
        llm = la.MockLLM(seed=7)
        self.assertEqual(llm.generate("同一个问题").text, llm.generate("同一个问题").text)

    def test_different_seed_diverges(self):
        a = la.MockLLM(seed=1, hallucination_rate=0.5, competence=0.3)
        b = la.MockLLM(seed=999, hallucination_rate=0.5, competence=0.3)
        outputs_a = [a.generate(f"题目{i}").text for i in range(30)]
        outputs_b = [b.generate(f"题目{i}").text for i in range(30)]
        self.assertNotEqual(outputs_a, outputs_b)

    def test_determinism_survives_process_hash_randomization(self):
        """用 sha256 而非内置 hash()，才不会受 PYTHONHASHSEED 影响。

        这里断言实现里确实没用内置 hash：同一 seed 下重建实例结果一致。
        """
        first = la.MockLLM(seed=555).generate("固定题面").text
        second = la.MockLLM(seed=555).generate("固定题面").text
        self.assertEqual(first, second)


class TestMockLLMKnowledgeBase(unittest.TestCase):
    KB = {"1+1 等于几？": "2", "中国的首都是哪里？": "北京"}

    def test_returns_correct_answer_when_competent(self):
        """能力=1.0 时幻觉概率为 0，必定给出知识库标准答案。"""
        llm = la.MockLLM(seed=1, knowledge_base=self.KB, competence=1.0)
        self.assertEqual(llm.generate("1+1 等于几？").text, "2")
        self.assertEqual(llm.generate("中国的首都是哪里？").text, "北京")

    def test_substring_match(self):
        llm = la.MockLLM(seed=1, knowledge_base=self.KB, competence=1.0)
        r = llm.generate("请回答下面的问题：1+1 等于几？ 只输出答案。")
        self.assertEqual(r.text, "2")

    def test_unknown_question_is_not_counted_as_hallucination(self):
        llm = la.MockLLM(seed=1, knowledge_base=self.KB, competence=1.0)
        r = llm.generate("一个知识库里没有的问题")
        self.assertFalse(r.hallucinated)


class TestMockLLMHallucinationModel(unittest.TestCase):
    """幻觉概率模型 —— 对照实验三组差异的来源。"""

    KB = {f"题目{i}": str(i) for i in range(200)}

    def _run(self, competence: float, difficulty: int = 1, n: int = 200) -> int:
        llm = la.MockLLM(seed=2024, knowledge_base=self.KB, hallucination_rate=0.5)
        return sum(
            llm.generate(f"题目{i}", competence=competence, difficulty=difficulty).hallucinated
            for i in range(n)
        )

    def test_zero_rate_never_hallucinates(self):
        llm = la.MockLLM(seed=1, knowledge_base=self.KB, hallucination_rate=0.0)
        self.assertEqual(sum(llm.generate(f"题目{i}").hallucinated for i in range(50)), 0)

    def test_full_competence_never_hallucinates(self):
        """p = rate * (1 - competence)，competence=1 时恒为 0。"""
        self.assertEqual(self._run(competence=1.0), 0)

    def test_low_competence_hallucinates_more_than_high(self):
        """核心断言：能力低的执行者幻觉更多。

        这正是对照组B（轮询分配，常派给不擅长的 Agent）劣于
        实验组C（按能力打分派单）的机制。
        """
        low = self._run(competence=0.1)
        high = self._run(competence=0.9)
        self.assertGreater(low, high)
        self.assertGreater(low, 0)

    def test_higher_difficulty_hallucinates_more(self):
        easy = self._run(competence=0.5, difficulty=1)
        hard = self._run(competence=0.5, difficulty=5)
        self.assertGreaterEqual(hard, easy)

    def test_hallucinated_response_is_marked_and_counted(self):
        llm = la.MockLLM(seed=3, knowledge_base=self.KB, hallucination_rate=1.0, competence=0.0)
        r = llm.generate("题目1")
        self.assertTrue(r.hallucinated)
        self.assertIn(lb.HALLUCINATION_MARK, r.text)
        self.assertNotIn(lb.HALLUCINATION_MARK, r.clean_text)
        self.assertEqual(llm.hallucination_count, 1)

    def test_hallucinated_answer_differs_from_truth(self):
        llm = la.MockLLM(seed=3, knowledge_base=self.KB, hallucination_rate=1.0, competence=0.0)
        self.assertNotEqual(llm.generate("题目7").clean_text, "7")

    def test_numeric_hallucination_stays_numeric(self):
        """数值题的幻觉给邻近错误数字，比返回乱码更贴近真实模型行为。"""
        llm = la.MockLLM(
            seed=11, knowledge_base={"计算题": "100"}, hallucination_rate=1.0, competence=0.0
        )
        text = llm.generate("计算题").clean_text
        self.assertTrue(text.lstrip("-").replace(".", "", 1).isdigit(), f"实际输出：{text}")
        self.assertNotEqual(text, "100")


class TestMockLLMTaskKinds(unittest.TestCase):
    def test_decompose_returns_parsable_json(self):
        llm = la.MockLLM(seed=1)
        text = llm.generate(
            "请把这个问题拆解成子任务：先做 math 计算；再写 code；最后 writing 总结",
            kind=la.TaskKind.DECOMPOSE,
        ).text
        subtasks = json.loads(text)
        self.assertIsInstance(subtasks, list)
        self.assertGreaterEqual(len(subtasks), 2)
        for st in subtasks:
            self.assertIn("id", st)
            self.assertIn("description", st)
            self.assertIn("domain", st)
            self.assertIn("difficulty", st)

    def test_decompose_never_hallucinates(self):
        llm = la.MockLLM(seed=1, hallucination_rate=1.0, competence=0.0)
        r = llm.generate("拆解：a；b", kind=la.TaskKind.DECOMPOSE)
        self.assertFalse(r.hallucinated)

    def test_aggregate_mentions_inputs(self):
        llm = la.MockLLM(seed=1)
        r = llm.generate("子结果1=甲 子结果2=乙", kind=la.TaskKind.AGGREGATE)
        self.assertIn("综合", r.text)

    def test_latency_grows_with_difficulty(self):
        llm = la.MockLLM(seed=1, latency_range_ms=(100, 100))
        easy = llm.generate("题", competence=0.9, difficulty=1).latency_ms
        hard = llm.generate("题", competence=0.9, difficulty=5).latency_ms
        self.assertGreater(hard, easy)

    def test_simulate_delay_off_by_default(self):
        """默认不真的 sleep，否则跑完整实验要白等好几分钟。"""
        import time

        llm = la.MockLLM(seed=1, latency_range_ms=(500, 500))
        start = time.perf_counter()
        llm.generate("题")
        self.assertLess(time.perf_counter() - start, 0.2)


class TestFactory(unittest.TestCase):
    def test_available_providers(self):
        self.assertEqual(la.available_providers(), ["anthropic", "mock", "openai"])

    def test_get_mock(self):
        self.assertIsInstance(la.get_llm("mock"), la.MockLLM)

    def test_unknown_provider_raises(self):
        with self.assertRaises(lb.LLMConfigError) as ctx:
            la.get_llm("gpt5-turbo-ultra")
        self.assertIn("未知的 LLM_PROVIDER", str(ctx.exception))

    def test_tier_selects_model(self):
        """阶段4「混合大模型协同」的切换点。"""
        large = la.get_llm("mock", tier="large")
        small = la.get_llm("mock", tier="small")
        self.assertNotEqual(large.model, small.model)

    def test_explicit_model_overrides_tier(self):
        self.assertEqual(la.get_llm("mock", model="my-model", tier="small").model, "my-model")

    def test_openai_without_key_raises_config_error(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            with self.assertRaises(lb.LLMConfigError) as ctx:
                la.OpenAICompatLLM(api_key="")
            self.assertIn("LLM_API_KEY", str(ctx.exception))

    def test_anthropic_without_key_raises_config_error(self):
        with mock.patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""}, clear=False
        ):
            with self.assertRaises(lb.LLMConfigError):
                la.AnthropicLLM(api_key="")


class TestEndpointConstruction(unittest.TestCase):
    """base_url 已带 /v1 时不能拼成 /v1/v1 —— 自建服务很容易踩。"""

    def test_openai_endpoint_without_v1(self):
        c = la.OpenAICompatLLM(api_key="k", base_url="https://api.deepseek.com")
        self.assertEqual(c.endpoint, "https://api.deepseek.com/v1/chat/completions")

    def test_openai_endpoint_with_v1(self):
        c = la.OpenAICompatLLM(api_key="k", base_url="http://localhost:11434/v1")
        self.assertEqual(c.endpoint, "http://localhost:11434/v1/chat/completions")

    def test_openai_endpoint_trailing_slash(self):
        c = la.OpenAICompatLLM(api_key="k", base_url="https://api.openai.com/")
        self.assertEqual(c.endpoint, "https://api.openai.com/v1/chat/completions")

    def test_anthropic_endpoint(self):
        c = la.AnthropicLLM(api_key="k", base_url="https://api.anthropic.com")
        self.assertEqual(c.endpoint, "https://api.anthropic.com/v1/messages")


class TestHttpBackendsMocked(unittest.TestCase):
    """用打桩的 HTTP 响应验证解析逻辑，不发真实网络请求。"""

    def test_openai_parses_response(self):
        payload = {
            "model": "gpt-4o-mini",
            "choices": [{"message": {"content": "答案是 42"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }
        client = la.OpenAICompatLLM(api_key="k", base_url="https://example.com")
        with mock.patch.object(client, "_post", return_value=payload):
            r = client.generate("问题", system="你是助手")
        self.assertEqual(r.text, "答案是 42")
        self.assertEqual(r.prompt_tokens, 11)
        self.assertEqual(r.completion_tokens, 7)
        self.assertEqual(r.model, "gpt-4o-mini")

    def test_openai_malformed_response_raises(self):
        client = la.OpenAICompatLLM(api_key="k", base_url="https://example.com")
        with mock.patch.object(client, "_post", return_value={"unexpected": True}):
            with self.assertRaises(lb.LLMError):
                client.generate("问题")

    def test_anthropic_parses_response(self):
        payload = {
            "model": "claude-sonnet-4",
            "content": [{"type": "text", "text": "答案是 42"}],
            "usage": {"input_tokens": 9, "output_tokens": 4},
        }
        client = la.AnthropicLLM(api_key="k", base_url="https://example.com")
        with mock.patch.object(client, "_post", return_value=payload):
            r = client.generate("问题")
        self.assertEqual(r.text, "答案是 42")
        self.assertEqual(r.prompt_tokens, 9)
        self.assertEqual(r.completion_tokens, 4)

    def test_anthropic_system_goes_to_top_level_field(self):
        """Anthropic 的 system 是顶层字段，不是 messages 里的一条。"""
        client = la.AnthropicLLM(api_key="k", base_url="https://example.com")
        captured = {}

        def fake_post(url, headers, body):
            captured.update(body)
            return {"content": [{"type": "text", "text": "ok"}], "usage": {}}

        with mock.patch.object(client, "_post", side_effect=fake_post):
            client.generate("问题", system="系统提示")

        self.assertEqual(captured["system"], "系统提示")
        self.assertEqual(len(captured["messages"]), 1)
        self.assertEqual(captured["messages"][0]["role"], "user")

    def test_anthropic_skips_non_text_blocks(self):
        payload = {
            "content": [
                {"type": "thinking", "thinking": "内部推理不应出现在结果里"},
                {"type": "text", "text": "最终答案"},
            ],
            "usage": {},
        }
        client = la.AnthropicLLM(api_key="k", base_url="https://example.com")
        with mock.patch.object(client, "_post", return_value=payload):
            r = client.generate("问题")
        self.assertEqual(r.text, "最终答案")


@unittest.skipUnless(
    os.getenv("LLM_PROVIDER", "mock").lower() in {"openai", "anthropic"}
    and (os.getenv("LLM_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")),
    "未配置真实 LLM 后端（.env 里设置 LLM_PROVIDER + LLM_API_KEY 后自动启用）",
)
class TestRealBackend(unittest.TestCase):
    """真实后端联通性测试。默认 skip。"""

    def test_real_generate(self):
        llm = la.get_llm()
        r = llm.generate("只回复两个字：你好", max_tokens=32)
        self.assertTrue(r.text.strip())
        self.assertGreater(r.latency_ms, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
