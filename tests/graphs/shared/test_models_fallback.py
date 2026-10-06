"""Fallback chain loading (shared/models.py)."""

import structlog

import shared.models as m
from shared.models import load_chat_model_with_fallbacks


def test_no_fallbacks_returns_plain_model(monkeypatch) -> None:
    monkeypatch.setattr(m, "load_chat_model", lambda name, **kw: f"model:{name}")
    assert load_chat_model_with_fallbacks("openai/gpt-4o-mini", []) == "model:openai/gpt-4o-mini"


def test_fallbacks_build_chain_in_order(monkeypatch) -> None:
    class _Fake:
        def __init__(self, name):
            self.name = name

        def with_fallbacks(self, others):
            return ("chain", self.name, tuple(others))

    monkeypatch.setattr(m, "load_chat_model", lambda name, **kw: _Fake(name))
    chain = load_chat_model_with_fallbacks("openai/gpt-4o", ["openai/gpt-4o-mini", "anthropic/claude-haiku"])
    assert chain[1] == "openai/gpt-4o"
    assert [f.name for f in chain[2]] == ["openai/gpt-4o-mini", "anthropic/claude-haiku"]


def test_correlation_headers_carry_the_run_ids() -> None:
    """The run's ids ride along as HTTP headers so a proxy can filter one run's
    calls (`~hq"x-run-id: <uuid>"`) and the same id searches the trace UI."""
    structlog.contextvars.bind_contextvars(run_id="r-1", thread_id="t-1")
    try:
        assert m._correlation_headers() == {"x-run-id": "r-1", "x-thread-id": "t-1"}
    finally:
        structlog.contextvars.clear_contextvars()


def test_client_kwargs_skip_headers_outside_a_run() -> None:
    """Import time and plain scripts have no run context — adding empty
    headers there would stamp every call with nothing."""
    structlog.contextvars.clear_contextvars()
    assert m._correlation_headers() == {}
    assert "default_headers" not in m._client_kwargs(None)
    assert m._client_kwargs({"reasoning_effort": "none"})["extra_body"] == {"reasoning_effort": "none"}


def test_client_kwargs_always_ask_for_response_headers() -> None:
    """The provider's per-call id (DeepSeek's x-ds-trace-id) rides in
    response_metadata, which the trace exporter serializes — that is what maps
    one Langfuse generation to exactly one proxied request."""
    structlog.contextvars.clear_contextvars()
    assert m._client_kwargs(None)["include_response_headers"] is True


def test_client_kwargs_keep_provider_fields_and_headers_together() -> None:
    structlog.contextvars.bind_contextvars(run_id="r-2", thread_id="t-2")
    try:
        kwargs = m._client_kwargs({"reasoning_effort": "none"})
        assert kwargs["extra_body"] == {"reasoning_effort": "none"}
        assert kwargs["default_headers"] == {"x-run-id": "r-2", "x-thread-id": "t-2"}
        assert kwargs["include_response_headers"] is True
        assert len(kwargs["callbacks"]) == 1
    finally:
        structlog.contextvars.clear_contextvars()


def test_trim_keeps_only_the_provider_trace_id() -> None:
    metadata = {"headers": {"x-ds-trace-id": "abc123", "server": "openresty"}, "finish_reason": "stop"}
    m._trim_response_headers(metadata)
    assert metadata == {"headers": {"x-ds-trace-id": "abc123"}, "finish_reason": "stop"}


def test_trim_drops_the_headers_key_when_nothing_is_kept() -> None:
    metadata = {"headers": {"server": "openresty"}}
    m._trim_response_headers(metadata)
    assert "headers" not in metadata


def test_trim_is_a_noop_without_captured_headers() -> None:
    metadata = {"finish_reason": "stop"}
    m._trim_response_headers(metadata)
    assert metadata == {"finish_reason": "stop"}


def test_caller_code_location_points_at_the_calling_module() -> None:
    """Trace UIs name spans after nodes, not files — this is what turns a span
    into a location (`code.filepath`), skipping langchain's own frames."""
    location = m._caller_code_location()
    assert location is not None
    path, lineno, function = location
    assert path.endswith("test_models_fallback.py")
    assert function == "test_caller_code_location_points_at_the_calling_module"
    assert lineno > 0


def test_stamp_code_location_is_a_noop_without_an_active_span() -> None:
    """No recording span (tracing off) — must not raise, and must not pay for
    the stack walk."""
    m._stamp_code_location()


def test_extra_body_reaches_every_model_in_the_chain(monkeypatch) -> None:
    """Per-call provider fields (e.g. disabling a reasoning mode) must apply to
    the fallback models too, or a mid-chain switch silently changes behaviour."""
    seen: list[tuple[str, dict | None]] = []

    class _Fake:
        def with_fallbacks(self, others):
            return self

    def _load(name: str, *, extra_body: dict | None = None):
        seen.append((name, extra_body))
        return _Fake()

    monkeypatch.setattr(m, "load_chat_model", _load)
    load_chat_model_with_fallbacks(
        "openai/deepseek-flash",
        ["openai/deepseek-v4-pro"],
        extra_body={"reasoning_effort": "none"},
    )
    assert seen == [
        ("openai/deepseek-flash", {"reasoning_effort": "none"}),
        ("openai/deepseek-v4-pro", {"reasoning_effort": "none"}),
    ]
