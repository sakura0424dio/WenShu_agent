from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from src.config import AppConfig
from src.domain.catalog import Catalog
from src.domain.models import AgentAction


class ModelUnavailableError(RuntimeError):
    pass


class ModelOutputError(RuntimeError):
    pass


@dataclass
class AgentContext:
    state: dict[str, Any]
    catalog: Catalog


class ModelAdapter(Protocol):
    mode: str

    def next_action(self, context: AgentContext) -> AgentAction: ...


class UnavailableModel:
    """配置了真实模型但不可用时，保留服务可启动并如实返回未完成。"""

    mode = "deepseek"

    def __init__(self, message: str) -> None:
        self.message = message

    def next_action(self, context: AgentContext) -> AgentAction:
        raise ModelUnavailableError(self.message)


def build_agent(config: AppConfig, mode: str) -> ModelAdapter:
    if mode == "deepseek":
        from src.agents.deepseek_model import DeepSeekModel

        try:
            return DeepSeekModel(config)
        except ModelUnavailableError as exc:
            return UnavailableModel(str(exc))
    from src.agents.mock_model import MockModel

    return MockModel()
