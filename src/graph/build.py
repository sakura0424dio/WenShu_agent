from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from src.graph.nodes import (
    GraphDeps,
    agent_node,
    execute_node,
    finalize_node,
    guard_node,
    prepare_node,
    route_after_execute,
    route_after_guard,
    route_after_prepare,
)
from src.graph.state import RunState


def build_graph(deps: GraphDeps, checkpointer: Any | None = None):
    builder = StateGraph(RunState)

    builder.add_node("prepare", lambda state: prepare_node(state, deps))
    builder.add_node("agent", lambda state: agent_node(state, deps))
    builder.add_node("guard", lambda state: guard_node(state, deps))
    builder.add_node("execute_tool", lambda state: execute_node(state, deps))
    builder.add_node("finalize", lambda state: finalize_node(state, deps))

    builder.add_edge(START, "prepare")
    builder.add_conditional_edges(
        "prepare",
        route_after_prepare,
        {"agent": "agent", "finalize": "finalize"},
    )
    builder.add_edge("agent", "guard")
    builder.add_conditional_edges(
        "guard",
        route_after_guard,
        {"agent": "agent", "execute": "execute_tool", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "execute_tool",
        route_after_execute,
        {"agent": "agent", "finalize": "finalize"},
    )
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer)
