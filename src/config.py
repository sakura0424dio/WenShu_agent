from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.yaml"
EXAMPLE_CONFIG_PATH = ROOT_DIR / "config.example.yaml"


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    host: str = "127.0.0.1"
    port: int = 8000


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    mode: Literal["mock", "deepseek"] = "mock"
    provider: str = "deepseek"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    name: str = "deepseek-chat"
    temperature: float = 0.0
    timeout_seconds: int = 30
    max_tokens: int = 1200


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    max_steps: int = 12
    max_tool_calls: int = 8
    max_steps_hard: int = 20
    max_tool_calls_hard: int = 12
    recursion_limit: int = 80
    run_timeout_seconds: int = 120


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    database_path: str = "storage/app.db"
    checkpoint_path: str = "storage/checkpoints.sqlite3"
    plans_dir: str = "storage/plans"
    traces_dir: str = "examples/traces"


class DemoConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    fixed_now: str = ""
    allow_failure_injection: bool = True


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    server: ServerConfig = Field(default_factory=ServerConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    demo: DemoConfig = Field(default_factory=DemoConfig)

    def resolve_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else ROOT_DIR / path


def _load_dotenv(path: Path) -> None:
    """极简 .env 加载：不覆盖已存在的环境变量，不引入额外依赖。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _apply_env(config: AppConfig) -> AppConfig:
    raw = config.model_dump()
    if os.getenv("MODEL_MODE"):
        raw["model"]["mode"] = os.environ["MODEL_MODE"]
    if os.getenv("DEEPSEEK_API_KEY"):
        raw["model"]["api_key"] = os.environ["DEEPSEEK_API_KEY"]
    if os.getenv("DEEPSEEK_BASE_URL"):
        raw["model"]["base_url"] = os.environ["DEEPSEEK_BASE_URL"]
    if os.getenv("DEEPSEEK_MODEL"):
        raw["model"]["name"] = os.environ["DEEPSEEK_MODEL"]
    if os.getenv("APP_FIXED_NOW"):
        raw["demo"]["fixed_now"] = os.environ["APP_FIXED_NOW"]
    if os.getenv("DATABASE_PATH"):
        raw["storage"]["database_path"] = os.environ["DATABASE_PATH"]
    if os.getenv("CHECKPOINT_PATH"):
        raw["storage"]["checkpoint_path"] = os.environ["CHECKPOINT_PATH"]
    if os.getenv("ENABLE_FAILURE_INJECTION"):
        raw["demo"]["allow_failure_injection"] = os.environ[
            "ENABLE_FAILURE_INJECTION"
        ].lower() in {"1", "true", "yes", "on"}
    # 重新校验，避免非法环境变量绕过 Literal/类型约束。
    return AppConfig.model_validate(raw)


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    _load_dotenv(ROOT_DIR / ".env")
    configured_path = os.getenv("APP_CONFIG")
    path = Path(configured_path) if configured_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        path = EXAMPLE_CONFIG_PATH
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return _apply_env(AppConfig.model_validate(raw))


def reset_config_cache() -> None:
    """测试辅助：清除配置缓存。"""
    get_config.cache_clear()
