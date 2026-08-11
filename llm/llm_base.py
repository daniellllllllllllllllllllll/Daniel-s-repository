"""LLM 抽象基类与公共数据结构。

所有具体后端（Mock / OpenAI 兼容 / Anthropic）都实现同一个 :class:`BaseLLM`
接口，上层 Agent 与 Controller 只依赖这个接口，不关心底下接的是哪家模型。
这样做的直接收益：

* 阶段3 对照实验可以用 :class:`~llm.llm_api.MockLLM` 得到**可复现**的指标；
* 阶段4「混合大模型协同」只需在工厂函数里按 tier 返回不同实例，
  Agent 代码一行都不用改。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from config import get_logger

logger = get_logger("blkmac.llm")

__all__ = [
    "LLMError",
    "LLMConfigError",
    "LLMTimeout",
    "LLMResponse",
    "BaseLLM",
    "retry_on_error",
    "HALLUCINATION_MARK",
]

# MockLLM 在模拟幻觉时会把这个标记塞进回答里，
# 对照实验据此统计"幻觉次数"。真实后端不会产生该标记，
# 那时由 Controller 的答案比对逻辑判定幻觉。
HALLUCINATION_MARK = "__HALLUCINATION__"


class LLMError(Exception):
    """LLM 调用失败的基类。"""


class LLMConfigError(LLMError):
    """配置缺失或非法，例如选了 openai 后端却没填 API Key。"""


class LLMTimeout(LLMError):
    """请求超时。"""


@dataclass
class LLMResponse:
    """一次 LLM 调用的完整结果。

    ``latency_ms`` 由基类统一测量，是阶段3「平均耗时」指标的数据来源。
    """

    text: str
    provider: str
    model: str
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    hallucinated: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def clean_text(self) -> str:
        """去掉幻觉标记后的可读文本（写进结果/上链前使用）。"""
        return self.text.replace(HALLUCINATION_MARK, "").strip()

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": round(self.latency_ms, 2),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "hallucinated": self.hallucinated,
        }


def retry_on_error(
    max_retries: int = 3,
    base_delay: float = 0.5,
    retry_on: tuple[type[Exception], ...] = (LLMError,),
) -> Callable:
    """指数退避重试装饰器。

    只重试可恢复的错误（网络抖动、限流）；:class:`LLMConfigError` 属于
    配置问题，重试多少次都不会好，因此直接放行不重试。
    """

    def decorator(func: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except LLMConfigError:
                    raise  # 配置错误无需重试
                except retry_on as exc:
                    last_exc = exc
                    if attempt >= max_retries:
                        break
                    delay = base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "LLM 调用失败（第 %d/%d 次），%.1fs 后重试：%s",
                        attempt,
                        max_retries,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
            raise LLMError(f"重试 {max_retries} 次后仍然失败：{last_exc}") from last_exc

        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        return wrapper

    return decorator


class BaseLLM(ABC):
    """所有 LLM 后端的统一接口。

    子类只需实现 :meth:`_complete`（纯粹的"给提示词、拿文本"），
    计时、统计、异常包装都由基类的 :meth:`generate` 负责。
    """

    provider: str = "base"

    def __init__(self, model: str, temperature: float = 0.2, max_tokens: int = 1024) -> None:
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.call_count = 0
        self.total_latency_ms = 0.0

    # -- 子类实现 ---------------------------------------------------------

    @abstractmethod
    def _complete(self, prompt: str, system: str | None, **kwargs) -> LLMResponse:
        """真正发起一次补全。子类不需要自己计时。"""

    # -- 对外接口 ---------------------------------------------------------

    def generate(self, prompt: str, system: str | None = None, **kwargs) -> LLMResponse:
        """生成回答，统一计时与计数。"""
        if not prompt or not prompt.strip():
            raise ValueError("prompt 不能为空")

        start = time.perf_counter()
        response = self._complete(prompt, system, **kwargs)
        elapsed_ms = (time.perf_counter() - start) * 1000

        # 子类若已自行填写 latency（如 Mock 模拟延迟）则保留，否则用实测值
        if not response.latency_ms:
            response.latency_ms = elapsed_ms

        self.call_count += 1
        self.total_latency_ms += response.latency_ms
        return response

    def generate_text(self, prompt: str, system: str | None = None, **kwargs) -> str:
        """只要文本的便捷方法。"""
        return self.generate(prompt, system, **kwargs).clean_text

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.call_count if self.call_count else 0.0

    def reset_stats(self) -> None:
        self.call_count = 0
        self.total_latency_ms = 0.0

    def health_check(self) -> bool:
        """探活。真实后端会实际发一次极短请求。"""
        try:
            self.generate("ping", max_tokens=8)
            return True
        except LLMError:
            return False

    def __repr__(self) -> str:
        return f"<{type(self).__name__} provider={self.provider} model={self.model}>"
