"""LLM 后端具体实现与工厂函数。

提供三种后端：

============  ==========================================================
provider      说明
============  ==========================================================
``mock``      内置确定性模拟模型。**对照实验默认使用**，零成本、可复现。
``openai``    任何 OpenAI 兼容的 ``/v1/chat/completions`` 端点，
              涵盖 OpenAI / DeepSeek / Moonshot / vLLM / Ollama / LM Studio。
``anthropic`` Anthropic ``/v1/messages`` 端点。
============  ==========================================================

切换方式：改 ``.env`` 里的 ``LLM_PROVIDER``，代码无需改动。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from typing import Any

import requests

from config import get_logger, settings
from llm.llm_base import (
    HALLUCINATION_MARK,
    BaseLLM,
    LLMConfigError,
    LLMError,
    LLMResponse,
    LLMTimeout,
    retry_on_error,
)

logger = get_logger("blkmac.llm.api")

__all__ = [
    "MockLLM",
    "OpenAICompatLLM",
    "AnthropicLLM",
    "get_llm",
    "available_providers",
    "TaskKind",
]


class TaskKind:
    """调用意图。显式传入比让模型去猜提示词类型可靠得多。"""

    DECOMPOSE = "decompose"  # 拆解复杂问题为子任务
    ANSWER = "answer"  # 回答一个子问题
    AGGREGATE = "aggregate"  # 汇总子结果
    GENERIC = "generic"


# ===========================================================================
# MockLLM —— 确定性模拟模型
# ===========================================================================


class MockLLM(BaseLLM):
    """离线确定性模拟模型，专为对照实验设计。

    为什么需要它
    ------------
    对照实验要比较三种架构的优劣。如果直接接真实大模型，每次运行的正确率都会
    因采样随机性而漂移几个百分点，读者无法判断差异究竟来自架构还是来自噪声。
    MockLLM 用固定种子把"模型能力"这一变量**钉死**，让图表上的差异只可能来自
    任务分配策略本身。

    幻觉模型
    --------
    一次回答是否产生幻觉，由下式的概率决定::

        p = base_rate * (1 - competence) * (1 + 0.25 * (difficulty - 1))

    * ``competence``：执行者在该领域的能力值（0~1），由 Controller 传入。
      对照组B 轮询分配，经常把 math 任务派给不擅长 math 的 Agent，
      competence 低 → 幻觉概率高；实验组C 按能力打分派单，competence 高 → 幻觉少。
    * ``difficulty``：任务难度 1~5，越难越容易出错。

    随机性完全由 ``seed + 提示词内容`` 决定，因此同样的输入永远得到同样的输出。

    .. note::
       这是**受控模拟**，不是真实模型行为。论文写作时须说明这一点；
       把 ``LLM_PROVIDER`` 换成 openai/anthropic 即可用真实模型复跑同一套流程。
    """

    provider = "mock"

    def __init__(
        self,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        seed: int | None = None,
        hallucination_rate: float | None = None,
        competence: float = 0.7,
        knowledge_base: dict[str, str] | None = None,
        decomposition_plan: dict[str, list[dict]] | None = None,
        latency_range_ms: tuple[int, int] | None = None,
        simulate_delay: bool = False,
    ) -> None:
        super().__init__(model or settings.LLM_MODEL, temperature, max_tokens)
        self.seed = seed if seed is not None else settings.MOCK_SEED
        self.hallucination_rate = (
            hallucination_rate
            if hallucination_rate is not None
            else settings.MOCK_HALLUCINATION_RATE
        )
        self.competence = competence
        # 题面 -> 标准答案。模拟"模型掌握的知识"。
        self.knowledge_base = dict(knowledge_base or {})
        # 原题 -> 子任务列表。模拟"模型具备的拆解能力"。
        # 没有它的话，拆解出来的子任务描述不含原题内容，
        # 执行 Agent 就无从查知识库，三组正确率也就没法比。
        self.decomposition_plan = dict(decomposition_plan or {})
        self.latency_range_ms = latency_range_ms or (
            settings.MOCK_LATENCY_MIN_MS,
            settings.MOCK_LATENCY_MAX_MS,
        )
        # 默认不真的 sleep：20 题 x 3 组 x 多轮调用会让实验白等好几分钟，
        # 而 latency 指标用模拟值同样能反映架构差异。需要真实等待时置 True。
        self.simulate_delay = simulate_delay

        self.hallucination_count = 0

    # -- 内部工具 ---------------------------------------------------------

    def _rng(self, prompt: str, salt: str = "") -> random.Random:
        """由 seed + 提示词派生确定性随机源。

        用 sha256 而不是内置 ``hash()``：后者对 str 加了进程级随机盐
        （PYTHONHASHSEED），跨进程不稳定，会破坏可复现性。
        """
        h = hashlib.sha256(f"{self.seed}|{salt}|{prompt}".encode("utf-8")).digest()
        return random.Random(int.from_bytes(h[:8], "big"))

    def _hallucinate_probability(self, competence: float, difficulty: int) -> float:
        p = self.hallucination_rate * (1.0 - competence) * (1.0 + 0.25 * (difficulty - 1))
        return min(max(p, 0.0), 1.0)

    def _lookup_answer(self, prompt: str) -> str | None:
        """在知识库里找与提示词匹配的题目。优先精确匹配，其次最长子串匹配。"""
        if prompt in self.knowledge_base:
            return self.knowledge_base[prompt]
        best: tuple[int, str] | None = None
        for question, answer in self.knowledge_base.items():
            if question and question in prompt:
                if best is None or len(question) > best[0]:
                    best = (len(question), answer)
        return best[1] if best else None

    def _wrong_answer(self, rng: random.Random, correct: str | None) -> str:
        """构造一个"看起来合理但错误"的回答，模拟幻觉。"""
        if correct and correct.strip().lstrip("-").replace(".", "", 1).isdigit():
            try:  # 数值题：给一个邻近的错误数字，比返回乱码更贴近真实幻觉
                value = float(correct)
                delta = rng.choice([-3, -2, -1, 1, 2, 3]) or 1
                wrong = value + delta
                return str(int(wrong)) if float(wrong).is_integer() else f"{wrong:.2f}"
            except ValueError:
                pass
        fillers = [
            "根据相关资料，答案应为「未经证实的结论」。",
            "这个问题的标准答案是「无法确定的推测值」。",
            "综合分析后可以确认结果为「臆造的结论」。",
        ]
        return rng.choice(fillers)

    # -- 三类任务的模拟输出 -----------------------------------------------

    def _lookup_plan(self, prompt: str) -> list[dict] | None:
        """在拆解计划里找与提示词匹配的原题（最长匹配优先）。"""
        best: tuple[int, list[dict]] | None = None
        for question, parts in self.decomposition_plan.items():
            if question and question in prompt:
                if best is None or len(question) > best[0]:
                    best = (len(question), parts)
        return best[1] if best else None

    def _mock_decompose(self, prompt: str, rng: random.Random) -> str:
        """模拟任务拆解：返回 Controller 期望的 JSON 数组。

        真实模型在这一步输出 JSON 的稳定性是个已知痛点，
        因此 Controller 侧必须有容错解析（见 ControllerAgent._extract_json_array）。
        """
        plan = self._lookup_plan(prompt)
        if plan is not None:
            return json.dumps(
                [
                    {
                        "id": p.get("id") or f"S{i + 1}",
                        "description": p["description"],
                        "domain": p.get("domain", "knowledge"),
                        "difficulty": int(p.get("difficulty", 1)),
                    }
                    for i, p in enumerate(plan)
                ],
                ensure_ascii=False,
            )

        # 没有预设计划时，按分隔符切分原题，保留各段原文
        domains = ["math", "code", "logic", "knowledge", "writing"]
        body = prompt.split("：", 1)[-1]
        segments = [s.strip() for s in re.split(r"[；;]", body) if s.strip()]
        segments = segments[:4] or [body.strip() or prompt]

        hits = [d for d in domains if d in prompt.lower()] or [rng.choice(domains)]
        subtasks = [
            {
                "id": f"S{i + 1}",
                "description": seg,
                "domain": hits[i % len(hits)],
                "difficulty": rng.randint(1, 4),
            }
            for i, seg in enumerate(segments)
        ]
        return json.dumps(subtasks, ensure_ascii=False)

    def _mock_aggregate(self, prompt: str) -> str:
        """模拟结果汇总：抽出各子任务答案并拼接。

        Controller 传来的提示词里每条子结果形如 ``- 子任务 X：答案``，
        这里把答案抽出来串联，模拟一个称职的汇总器。
        直接截断提示词尾部会丢掉靠前的子答案，导致正确率评分失真。
        """
        answers = re.findall(r"-\s*子任务\s*[^：]*：\s*(.+)", prompt)
        answers = [a.strip() for a in answers if a.strip()]
        if answers:
            return "；".join(answers)
        return f"综合各子任务结论：{prompt[-300:]}"

    def _mock_answer(
        self, prompt: str, rng: random.Random, competence: float, difficulty: int
    ) -> tuple[str, bool]:
        correct = self._lookup_answer(prompt)
        p = self._hallucinate_probability(competence, difficulty)
        if rng.random() < p:
            return self._wrong_answer(rng, correct) + HALLUCINATION_MARK, True
        if correct is not None:
            return correct, False
        # 知识库里没有的问题：给一个中性回答，不算幻觉
        return f"（模拟回答）针对该问题的处理结果：{prompt.strip()[:80]}", False

    # -- BaseLLM 接口 ------------------------------------------------------

    def _complete(self, prompt: str, system: str | None, **kwargs) -> LLMResponse:
        kind = kwargs.get("kind", TaskKind.ANSWER)
        competence = float(kwargs.get("competence", self.competence))
        difficulty = int(kwargs.get("difficulty", 1))
        rng = self._rng(prompt, salt=f"{kind}|{competence:.3f}|{difficulty}")

        hallucinated = False
        if kind == TaskKind.DECOMPOSE:
            text = self._mock_decompose(prompt, rng)
        elif kind == TaskKind.AGGREGATE:
            text = self._mock_aggregate(prompt)
        else:
            text, hallucinated = self._mock_answer(prompt, rng, competence, difficulty)

        if hallucinated:
            self.hallucination_count += 1

        lo, hi = self.latency_range_ms
        # 难度越高、能力越低，模拟耗时越长
        latency = rng.uniform(lo, hi) * (1.0 + 0.15 * (difficulty - 1)) * (2.0 - competence)
        if self.simulate_delay:
            time.sleep(latency / 1000.0)

        return LLMResponse(
            text=text,
            provider=self.provider,
            model=self.model,
            latency_ms=latency,
            prompt_tokens=max(1, len(prompt) // 4),
            completion_tokens=max(1, len(text) // 4),
            hallucinated=hallucinated,
            raw={"kind": kind, "competence": competence, "difficulty": difficulty},
        )


# ===========================================================================
# 真实后端
# ===========================================================================


class _HttpLLM(BaseLLM):
    """基于 requests 的 HTTP 后端公共部分。"""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        temperature: float,
        max_tokens: int,
        timeout: int,
    ) -> None:
        super().__init__(model, temperature, max_tokens)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._session = requests.Session()

    def _post(self, url: str, headers: dict, body: dict) -> dict:
        try:
            resp = self._session.post(url, headers=headers, json=body, timeout=self.timeout)
        except requests.Timeout as exc:
            raise LLMTimeout(f"请求 {url} 超时（{self.timeout}s）") from exc
        except requests.RequestException as exc:
            raise LLMError(f"请求 {url} 失败：{exc}") from exc

        if resp.status_code >= 400:
            # 截断响应体，避免把上千行的错误页面糊到日志里
            raise LLMError(f"{url} 返回 HTTP {resp.status_code}：{resp.text[:500]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise LLMError(f"响应不是合法 JSON：{resp.text[:200]}") from exc


class OpenAICompatLLM(_HttpLLM):
    """OpenAI 兼容的 ``/v1/chat/completions`` 后端。

    适用于 OpenAI、DeepSeek、Moonshot、以及本地的 vLLM / Ollama / LM Studio。
    """

    provider = "openai"

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: int | None = None,
    ) -> None:
        base_url = base_url or settings.LLM_BASE_URL or "https://api.openai.com"
        api_key = api_key if api_key is not None else settings.LLM_API_KEY
        if not api_key:
            raise LLMConfigError(
                "LLM_PROVIDER=openai 需要 LLM_API_KEY。\n"
                "请在 .env 里填写，或改用 LLM_PROVIDER=mock 跑离线实验。\n"
                "（本地 Ollama/vLLM 无鉴权时可随便填一个非空值，如 LLM_API_KEY=local）"
            )
        super().__init__(
            model=model or settings.LLM_MODEL,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature if temperature is not None else settings.LLM_TEMPERATURE,
            max_tokens=max_tokens or settings.LLM_MAX_TOKENS,
            timeout=timeout or settings.LLM_TIMEOUT,
        )

    @property
    def endpoint(self) -> str:
        # base_url 可能已经带了 /v1（很多自建服务这么给），避免拼成 /v1/v1
        base = self.base_url
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    @retry_on_error(max_retries=settings.LLM_MAX_RETRIES)
    def _complete(self, prompt: str, system: str | None, **kwargs) -> LLMResponse:
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body = {
            "model": kwargs.get("model", self.model),
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        }
        data = self._post(
            self.endpoint,
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            body,
        )

        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"响应结构不符合 OpenAI 格式：{str(data)[:300]}") from exc

        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            provider=self.provider,
            model=data.get("model", self.model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            raw=data,
        )


class AnthropicLLM(_HttpLLM):
    """Anthropic ``/v1/messages`` 后端。

    未显式配置 ``LLM_BASE_URL`` / ``LLM_API_KEY`` 时，会回退读取环境变量
    ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN``。
    """

    provider = "anthropic"
    API_VERSION = "2023-06-01"

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: int | None = None,
    ) -> None:
        base_url = (
            base_url
            or settings.LLM_BASE_URL
            or os.getenv("ANTHROPIC_BASE_URL", "")
            or "https://api.anthropic.com"
        )
        api_key = (
            api_key
            if api_key is not None
            else (
                settings.LLM_API_KEY
                or os.getenv("ANTHROPIC_API_KEY", "")
                or os.getenv("ANTHROPIC_AUTH_TOKEN", "")
            )
        )
        if not api_key:
            raise LLMConfigError(
                "LLM_PROVIDER=anthropic 需要 LLM_API_KEY（或环境变量 "
                "ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN）。"
            )
        super().__init__(
            model=model or settings.LLM_MODEL,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature if temperature is not None else settings.LLM_TEMPERATURE,
            max_tokens=max_tokens or settings.LLM_MAX_TOKENS,
            timeout=timeout or settings.LLM_TIMEOUT,
        )

    @property
    def endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    @retry_on_error(max_retries=settings.LLM_MAX_RETRIES)
    def _complete(self, prompt: str, system: str | None, **kwargs) -> LLMResponse:
        body: dict[str, Any] = {
            "model": kwargs.get("model", self.model),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system  # Anthropic 的 system 是顶层字段，不是一条 message

        data = self._post(
            self.endpoint,
            {
                "x-api-key": self.api_key,
                "anthropic-version": self.API_VERSION,
                "Content-Type": "application/json",
            },
            body,
        )

        try:
            blocks = data["content"]
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        except (KeyError, TypeError) as exc:
            raise LLMError(f"响应结构不符合 Anthropic 格式：{str(data)[:300]}") from exc

        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            provider=self.provider,
            model=data.get("model", self.model),
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            raw=data,
        )


# ===========================================================================
# 工厂
# ===========================================================================

_REGISTRY: dict[str, type[BaseLLM]] = {
    "mock": MockLLM,
    "openai": OpenAICompatLLM,
    "anthropic": AnthropicLLM,
}


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


def get_llm(
    provider: str | None = None,
    model: str | None = None,
    tier: str = "large",
    **kwargs,
) -> BaseLLM:
    """按配置构造 LLM 实例。

    :param provider: ``mock`` / ``openai`` / ``anthropic``，缺省读 ``.env``
    :param model:    显式指定模型名，缺省按 ``tier`` 选择
    :param tier:     ``"large"`` 走 ``LLM_MODEL``，``"small"`` 走 ``LLM_MODEL_SMALL``

    ``tier`` 参数是**阶段4「混合大模型协同」的落点**：
    Controller 判定子任务简单时传 ``tier="small"``，复杂时传 ``tier="large"``，
    Agent 侧代码完全不用改。
    """
    provider = (provider or settings.LLM_PROVIDER).lower().strip()
    if provider not in _REGISTRY:
        raise LLMConfigError(
            f"未知的 LLM_PROVIDER={provider!r}，可选：{', '.join(available_providers())}"
        )

    if model is None:
        model = settings.LLM_MODEL_SMALL if tier == "small" else settings.LLM_MODEL

    cls = _REGISTRY[provider]
    logger.debug("创建 LLM 实例 provider=%s model=%s tier=%s", provider, model, tier)
    return cls(model=model, **kwargs)
