"""BlkMaC 全局配置加载器。

统一从项目根目录的 .env 读取配置，所有模块都应该通过本模块取值，
不要在各自文件里散落 ``os.getenv``——否则默认值会各写一份，改一处漏一处。

用法::

    from config import settings
    print(settings.RSA_KEY_BITS)

    # 或者按需读取单个键
    from config import get_int
    port = get_int("TCP_BASE_PORT", 9101)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 路径与 .env 加载
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# override=False：已存在的真实环境变量优先于 .env，方便 CI / 命令行临时覆盖
load_dotenv(ENV_PATH, override=False)


# ---------------------------------------------------------------------------
# 类型安全的取值函数
# ---------------------------------------------------------------------------

_TRUE_SET = {"1", "true", "yes", "y", "on"}
_FALSE_SET = {"0", "false", "no", "n", "off", ""}


def get_str(key: str, default: str = "") -> str:
    """读取字符串配置，自动去除首尾空白与行尾注释残留的引号。"""
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().strip('"').strip("'")


def get_int(key: str, default: int) -> int:
    raw = get_str(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"配置项 {key}={raw!r} 不是合法整数") from None


def get_float(key: str, default: float) -> float:
    raw = get_str(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"配置项 {key}={raw!r} 不是合法浮点数") from None


def get_bool(key: str, default: bool) -> bool:
    raw = get_str(key).lower()
    if raw in _TRUE_SET:
        return True
    if raw in _FALSE_SET:
        return default if raw == "" else False
    raise ValueError(f"配置项 {key}={raw!r} 不是合法布尔值（true/false）")


def get_path(key: str, default: str) -> Path:
    """相对路径一律相对项目根目录解析，避免受当前工作目录影响。"""
    raw = get_str(key, default) or default
    p = Path(raw)
    return p if p.is_absolute() else (BASE_DIR / p)


# ---------------------------------------------------------------------------
# 结构化配置对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """一次性快照当前 .env 的全部配置。"""

    # --- LLM ---
    LLM_PROVIDER: str = field(default_factory=lambda: get_str("LLM_PROVIDER", "mock").lower())
    LLM_MODEL: str = field(default_factory=lambda: get_str("LLM_MODEL", "mock-large"))
    LLM_MODEL_SMALL: str = field(default_factory=lambda: get_str("LLM_MODEL_SMALL", "mock-small"))
    LLM_BASE_URL: str = field(default_factory=lambda: get_str("LLM_BASE_URL", ""))
    LLM_API_KEY: str = field(default_factory=lambda: get_str("LLM_API_KEY", ""))
    LLM_TEMPERATURE: float = field(default_factory=lambda: get_float("LLM_TEMPERATURE", 0.2))
    LLM_MAX_TOKENS: int = field(default_factory=lambda: get_int("LLM_MAX_TOKENS", 1024))
    LLM_TIMEOUT: int = field(default_factory=lambda: get_int("LLM_TIMEOUT", 60))
    LLM_MAX_RETRIES: int = field(default_factory=lambda: get_int("LLM_MAX_RETRIES", 3))

    # --- MockLLM ---
    MOCK_HALLUCINATION_RATE: float = field(
        default_factory=lambda: get_float("MOCK_HALLUCINATION_RATE", 0.18)
    )
    MOCK_LATENCY_MIN_MS: int = field(default_factory=lambda: get_int("MOCK_LATENCY_MIN_MS", 40))
    MOCK_LATENCY_MAX_MS: int = field(default_factory=lambda: get_int("MOCK_LATENCY_MAX_MS", 180))
    MOCK_SEED: int = field(default_factory=lambda: get_int("MOCK_SEED", 20250728))

    # --- 区块链 ---
    CHAIN_ENABLED: bool = field(default_factory=lambda: get_bool("CHAIN_ENABLED", False))
    CHAIN_RPC_URL: str = field(
        default_factory=lambda: get_str("CHAIN_RPC_URL", "http://127.0.0.1:8545")
    )
    CHAIN_PRIVATE_KEY: str = field(default_factory=lambda: get_str("CHAIN_PRIVATE_KEY", ""))
    CONTRACT_ADDRESS: str = field(default_factory=lambda: get_str("CONTRACT_ADDRESS", ""))
    CHAIN_ID: int = field(default_factory=lambda: get_int("CHAIN_ID", 1337))
    CHAIN_GAS_LIMIT: int = field(default_factory=lambda: get_int("CHAIN_GAS_LIMIT", 1_000_000))
    CHAIN_TX_TIMEOUT: int = field(default_factory=lambda: get_int("CHAIN_TX_TIMEOUT", 120))
    LEDGER_BACKEND: str = field(default_factory=lambda: get_str("LEDGER_BACKEND", "eth").lower())

    # --- TCP ---
    TCP_HOST: str = field(default_factory=lambda: get_str("TCP_HOST", "127.0.0.1"))
    TCP_BASE_PORT: int = field(default_factory=lambda: get_int("TCP_BASE_PORT", 9101))
    TCP_TIMEOUT: int = field(default_factory=lambda: get_int("TCP_TIMEOUT", 30))
    TCP_MAX_FRAME: int = field(default_factory=lambda: get_int("TCP_MAX_FRAME", 4 * 1024 * 1024))

    # --- 密码学 ---
    RSA_KEY_BITS: int = field(default_factory=lambda: get_int("RSA_KEY_BITS", 2048))
    RSA_KEY_DIR: Path = field(default_factory=lambda: get_path("RSA_KEY_DIR", "crypto"))

    # --- 实验 ---
    DATASET_PATH: Path = field(
        default_factory=lambda: get_path("DATASET_PATH", "test_dataset/questions.json")
    )
    RESULTS_DIR: Path = field(default_factory=lambda: get_path("RESULTS_DIR", "results"))
    AGENT_COUNT: int = field(default_factory=lambda: get_int("AGENT_COUNT", 4))
    EXPERIMENT_SEED: int = field(default_factory=lambda: get_int("EXPERIMENT_SEED", 20250728))

    SCORE_W_CAPABILITY: float = field(default_factory=lambda: get_float("SCORE_W_CAPABILITY", 0.5))
    SCORE_W_LOAD: float = field(default_factory=lambda: get_float("SCORE_W_LOAD", 0.3))
    SCORE_W_SUCCESS: float = field(default_factory=lambda: get_float("SCORE_W_SUCCESS", 0.2))
    SCORE_W_DIFFICULTY: float = field(default_factory=lambda: get_float("SCORE_W_DIFFICULTY", 0.0))
    SCORE_W_DIFFICULTY_MATCH: float = field(
        default_factory=lambda: get_float("SCORE_W_DIFFICULTY_MATCH", 0.2)
    )
    SCORE_W_ERROR: float = field(default_factory=lambda: get_float("SCORE_W_ERROR", 0.1))
    SCORE_W_LATENCY: float = field(default_factory=lambda: get_float("SCORE_W_LATENCY", 0.05))
    SCORE_W_TRUST: float = field(default_factory=lambda: get_float("SCORE_W_TRUST", 0.15))
    TRUST_INITIAL_SCORE: float = field(
        default_factory=lambda: get_float("TRUST_INITIAL_SCORE", 0.5)
    )
    TRUST_SUCCESS_REWARD: float = field(
        default_factory=lambda: get_float("TRUST_SUCCESS_REWARD", 0.05)
    )
    TRUST_HALLUCINATION_PENALTY: float = field(
        default_factory=lambda: get_float("TRUST_HALLUCINATION_PENALTY", 0.15)
    )
    TRUST_FAILURE_PENALTY: float = field(
        default_factory=lambda: get_float("TRUST_FAILURE_PENALTY", 0.1)
    )

    # --- 相关错误感知的可靠调度与验证（PROPOSAL_CORRELATION.md）---
    # 相关性修正项权重；0.0 = 不启用（消融开关）
    SCORE_W_CORRELATION: float = field(
        default_factory=lambda: get_float("SCORE_W_CORRELATION", 0.0)
    )
    # 额外 LLM 调用预算；0 = 关闭冗余验证（默认，保证既有 A/B/C 行为不变）
    REDUNDANCY_BUDGET: int = field(default_factory=lambda: get_int("REDUNDANCY_BUDGET", 0))
    # error_correlation 从冷启动先验切换到热启动 Pearson 的最小样本数
    CORRELATION_MIN_SAMPLES: int = field(
        default_factory=lambda: get_int("CORRELATION_MIN_SAMPLES", 4)
    )
    # per-domain Beta 后验先验（α=β=1 → 均匀先验）
    BETA_PRIOR_ALPHA: float = field(default_factory=lambda: get_float("BETA_PRIOR_ALPHA", 1.0))
    BETA_PRIOR_BETA: float = field(default_factory=lambda: get_float("BETA_PRIOR_BETA", 1.0))

    # --- 日志 ---
    LOG_LEVEL: str = field(default_factory=lambda: get_str("LOG_LEVEL", "INFO").upper())


settings = Settings()


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

_LOG_CONFIGURED = False


def setup_logging(level: str | None = None) -> None:
    """幂等地初始化根日志。重复调用不会叠加 handler。"""
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return
    logging.basicConfig(
        level=getattr(logging, (level or settings.LOG_LEVEL), logging.INFO),
        format="%(asctime)s [%(levelname)-7s] %(name)-22s | %(message)s",
        datefmt="%H:%M:%S",
    )
    _LOG_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def reload_settings() -> Settings:
    """重新读取 .env 并刷新全局 settings（测试里改环境变量后调用）。"""
    global settings
    load_dotenv(ENV_PATH, override=True)
    settings = Settings()
    return settings


__all__ = [
    "BASE_DIR",
    "ENV_PATH",
    "Settings",
    "settings",
    "get_str",
    "get_int",
    "get_float",
    "get_bool",
    "get_path",
    "get_logger",
    "setup_logging",
    "reload_settings",
]
