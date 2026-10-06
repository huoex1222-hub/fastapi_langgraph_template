"""Unit tests for the classify node's history filtering.

The endpoint does not strictly enforce ``tool_choice``: a history that still
carries earlier tool calls makes the model mimic one of them instead of the
forced ``IntentResult``, and the parser fails with "Unknown tool type".
"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from shopping_agent.nodes import _classify_history


def _tool_call(name: str, call_id: str = "call_1") -> dict:
    return {"name": name, "args": {}, "id": call_id, "type": "tool_call"}


def test_drops_tool_results() -> None:
    messages = [HumanMessage("hi"), ToolMessage(content="{}", tool_call_id="call_1")]
    assert [m.type for m in _classify_history(messages)] == ["human"]


def test_strips_tool_calls_but_keeps_the_text() -> None:
    message = AIMessage(content="Here's what I found", tool_calls=[_tool_call("present_products")])
    (out,) = _classify_history([message])
    assert out.content == "Here's what I found"
    assert out.tool_calls == []


def test_drops_messages_that_only_carried_a_tool_call() -> None:
    assert _classify_history([AIMessage(content="", tool_calls=[_tool_call("search_products")])]) == []


def test_keeps_plain_turns_untouched() -> None:
    messages = [SystemMessage("s"), HumanMessage("h"), AIMessage("a")]
    assert [m.type for m in _classify_history(messages)] == ["system", "human", "ai"]


def test_filters_the_mix_that_broke_the_parser() -> None:
    """Shape captured from a failed run: an earlier (tool call, tool result)
    pair plus presentation results, followed by the new user message."""
    messages = [
        SystemMessage("classifier"),
        HumanMessage("I'm interested in the Acme Drip Coffee Maker"),
        AIMessage(content="I'll look that up in our catalog.", tool_calls=[_tool_call("search_products")]),
        ToolMessage(content='{"status": "ok"}', tool_call_id="call_1"),
        AIMessage(content="Here's what I found", tool_calls=[_tool_call("present_products", "call_2")]),
        ToolMessage(content='{"status": "ok"}', tool_call_id="call_2"),
        HumanMessage("I'm interested in the Acme Burr Grinder"),
    ]
    out = _classify_history(messages)
    assert [m.type for m in out] == ["system", "human", "ai", "ai", "human"]
    assert all(not m.tool_calls for m in out if isinstance(m, AIMessage))
    assert not any(isinstance(m, ToolMessage) for m in out)
