"""BlkMaC 大模型交互包：统一接口 + 可插拔后端（mock / openai / anthropic）。"""

from llm.llm_api import (
    AnthropicLLM,
    MockLLM,
    OpenAICompatLLM,
    TaskKind,
    available_providers,
    get_llm,
)
from llm.llm_base import (
    HALLUCINATION_MARK,
    BaseLLM,
    LLMConfigError,
    LLMError,
    LLMResponse,
    LLMTimeout,
    retry_on_error,
)

__all__ = [
    "AnthropicLLM",
    "BaseLLM",
    "HALLUCINATION_MARK",
    "LLMConfigError",
    "LLMError",
    "LLMResponse",
    "LLMTimeout",
    "MockLLM",
    "OpenAICompatLLM",
    "TaskKind",
    "available_providers",
    "get_llm",
    "retry_on_error",
]
