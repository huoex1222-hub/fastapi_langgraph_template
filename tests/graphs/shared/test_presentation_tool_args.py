"""Field-less presentation components must still receive injected tool args.

langchain treats a pydantic args schema with no fields as "a tool that takes no
arguments" and invokes the function with nothing at all — not even the state
and tool_call_id LangGraph injects — so calling the checkout summary tool blew
up with "missing 2 required positional arguments: 'state' and 'tool_call_id'".
"""

from shared.presentation import PresentationComponent, PresentationPayload, make_presentation_tool


class _EmptyPayload(PresentationPayload):
    """No model-facing fields: the payload is assembled server-side."""


class _FilledPayload(PresentationPayload):
    value: str = "x"


def _spec(payload_model: type[PresentationPayload]) -> PresentationComponent:
    return PresentationComponent(name="t", component="SomeComponent", payload_model=payload_model)


def test_field_less_component_gets_the_injected_params_in_its_schema() -> None:
    tool = make_presentation_tool(_spec(_EmptyPayload), backend=None, description="d")
    assert {"state", "tool_call_id"} <= set(tool.args_schema.model_fields)


def test_field_less_component_hides_the_injected_params_from_the_model() -> None:
    """The model must still see a no-argument tool."""
    tool = make_presentation_tool(_spec(_EmptyPayload), backend=None, description="d")
    assert not tool.tool_call_schema.model_fields


def test_filled_component_keeps_its_pydantic_schema() -> None:
    assert make_presentation_tool(_spec(_FilledPayload), backend=None, description="d").args_schema is _FilledPayload


async def test_field_less_tool_receives_the_injected_arguments() -> None:
    """The regression itself: through a real ToolNode this raised TypeError
    ("missing 2 required positional arguments: 'state' and 'tool_call_id'")."""
    from typing import Annotated, TypedDict

    from langchain_core.messages import AIMessage
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import add_messages
    from langgraph.prebuilt import ToolNode

    class _State(TypedDict):
        messages: Annotated[list, add_messages]
        presentations: list

    tool = make_presentation_tool(_spec(_EmptyPayload), backend=None, description="d")
    graph = StateGraph(_State)
    graph.add_node("tools", ToolNode([tool]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)

    out = await graph.compile().ainvoke(
        {
            "messages": [
                AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "call_1", "type": "tool_call"}])
            ],
            "presentations": [],
        }
    )

    assert out["presentations"][0]["component"] == "SomeComponent"
    assert out["messages"][-1].tool_call_id == "call_1"
