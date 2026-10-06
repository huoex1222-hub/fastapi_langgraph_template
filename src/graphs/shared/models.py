"""Chat model loading, shared across graphs.

Central place for provider/model selection. Extend here (model aliases,
per-tier defaults, fallback chains) instead of inside individual graphs.
"""

import inspect
from typing import Any, cast

import structlog
from langchain.chat_models import init_chat_model
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import LLMResult

from agent_server.infra.observability.span_enrichment import set_span_code_location

_LIBRARY_PATH_MARKERS = ("site-packages", "/lib/", "/lib64/")
_OWN_FILE = __file__.replace("\\", "/")


def _caller_code_location() -> tuple[str, int, str] | None:
    """Where the model is being built: (path relative to ``src/``, lineno, function).

    Walks out of this module and out of site-packages — the first remaining
    frame is the graph/domain code that asked for the model.
    """
    for frame in inspect.stack(context=0)[1:]:
        path = frame.filename.replace("\\", "/")
        if path == _OWN_FILE or any(marker in path.lower() for marker in _LIBRARY_PATH_MARKERS):
            continue
        parts = path.split("/src/", 1)
        return (parts[1] if len(parts) == 2 else path), frame.lineno, frame.function
    return None


# Response headers worth keeping on the message. Each provider names its
# per-call id differently; add more here as needed.
_PROVIDER_TRACE_HEADERS = frozenset({"x-ds-trace-id"})


def _trim_response_headers(response_metadata: dict[str, Any]) -> None:
    """Reduce captured response headers to the provider's trace id (in place).

    ``include_response_headers=True`` captures ~700 B of headers per call; only
    the per-call id is useful, and it travels with every AI message into the
    graph state and the checkpoint.
    """
    headers = response_metadata.get("headers")
    if not isinstance(headers, dict):
        return
    kept = {name: value for name, value in headers.items() if name.lower() in _PROVIDER_TRACE_HEADERS}
    if kept:
        response_metadata["headers"] = kept
    else:
        response_metadata.pop("headers", None)


class _TrimResponseHeaders(AsyncCallbackHandler):
    """Callback form of :func:`_trim_response_headers` — attached at client
    construction so it survives ``bind_tools``/``with_structured_output``."""

    async def on_llm_new_token(self, token: Any, *, chunk: Any = None, **kwargs: Any) -> None:
        # Streaming: the provider's headers ride on the FIRST chunk (both its
        # generation_info and its message's response_metadata), and the final
        # message is a NEW object built by aggregating chunks — so on_llm_end's
        # message is not the one the caller receives. Trim the chunk instead,
        # before the caller folds it in.
        for metadata in (
            getattr(chunk, "generation_info", None),
            getattr(getattr(chunk, "message", None), "response_metadata", None),
        ):
            if isinstance(metadata, dict):
                _trim_response_headers(metadata)

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for batch in response.generations:
            for generation in batch:
                metadata = getattr(getattr(generation, "message", None), "response_metadata", None)
                if isinstance(metadata, dict):
                    _trim_response_headers(metadata)


def _correlation_headers() -> dict[str, str]:
    """The current run's ids, for tying one LLM call back to its trace.

    Both executors bind ``run_id``/``thread_id`` to structlog contextvars for
    the whole run, so every client built here stamps its HTTP requests with
    them: in a proxy (mitmweb) ``~hq"x-run-id: <uuid>"`` filters down to one
    run's calls, and the same id searches Langfuse. Empty outside a run
    (import time, scripts), and callers drop it when empty.
    """
    ctx = structlog.contextvars.get_contextvars()
    pairs = (("x-run-id", ctx.get("run_id")), ("x-thread-id", ctx.get("thread_id")))
    return {name: str(value) for name, value in pairs if value}


def _client_kwargs(extra_body: dict[str, Any] | None) -> dict[str, Any]:
    """Per-client request options shared by every loader."""
    # Hand the code location to the span processor while the node frame is
    # still here: spans are created on langchain's callback thread, where the
    # graph stack is gone.
    set_span_code_location(_caller_code_location())
    kwargs: dict[str, Any] = {}
    if extra_body is not None:
        kwargs["extra_body"] = extra_body
    headers = _correlation_headers()
    if headers:
        kwargs["default_headers"] = headers
    # Ride the provider's per-call id (e.g. DeepSeek's x-ds-trace-id) into the
    # message's response_metadata, which the OTel instrumentation serializes into
    # the trace — so one generation in Langfuse maps to exactly one captured
    # request in a proxy, no timestamp guessing. The callback then trims the
    # captured headers down to just that id.
    kwargs["include_response_headers"] = True
    kwargs["callbacks"] = [_TrimResponseHeaders()]
    return kwargs


def load_chat_model(
    fully_specified_name: str,
    *,
    extra_body: dict[str, Any] | None = None,
) -> BaseChatModel:
    """Load a chat model from a fully specified name.

    Args:
        fully_specified_name (str): String in the format 'provider/model'.
        extra_body (dict | None): Provider-specific request fields merged into
            every call (e.g. disabling a provider's reasoning mode). Passed
            through untouched — keep provider quirks at the call site.
    """
    provider, model = fully_specified_name.split("/", maxsplit=1)
    return init_chat_model(model, model_provider=provider, **_client_kwargs(extra_body))


def _load_resilient(fully_specified_name: str, max_retries: int, request_timeout: float) -> BaseChatModel:
    """A single model with retries/timeouts built INTO the client (the model
    stays a real BaseChatModel — bind_tools/with_structured_output keep
    working, unlike .with_retry() wrappers which drop them)."""
    provider, model = fully_specified_name.split("/", maxsplit=1)
    return init_chat_model(
        model,
        model_provider=provider,
        max_retries=max_retries,  # tenacity exponential backoff inside the client
        timeout=request_timeout,  # per-request timeout
        **_client_kwargs(None),
    )


def load_chat_model_with_fallbacks(
    fully_specified_name: str,
    fallbacks: list[str],
    *,
    extra_body: dict[str, Any] | None = None,
) -> BaseChatModel:
    """Load a chat model with a circular fallback chain behind it.

    On provider errors (rate limits, 5xx) the call degrades down the chain
    before failing. Order matters: cheapest/most-available last.
    """
    model = load_chat_model(fully_specified_name, extra_body=extra_body)
    if not fallbacks:
        return model
    # with_fallbacks returns Runnable in the stubs; it stays a chat model at runtime.
    return cast("BaseChatModel", model.with_fallbacks([load_chat_model(f, extra_body=extra_body) for f in fallbacks]))


# ---------------------------------------------------------------------------
# Resilience: per-model retry with backoff + total budget across the chain
# ---------------------------------------------------------------------------


def load_resilient_chat_model(
    fully_specified_name: str,
    fallbacks: list[str],
    *,
    max_retries: int = 2,
    request_timeout: float = 60.0,
) -> BaseChatModel:
    """Fallback chain with per-model retry/backoff and request timeouts built in.

    with_fallbacks alone switches to the next model on the FIRST error and has
    no timeout — a hung provider stalls the run indefinitely. Here every model
    in the chain retries transient errors with exponential backoff and bounds
    each request (pattern from fastapi-langgraph production template). The
    result stays a real chat model: bind_tools/with_structured_output work.
    """
    primary = _load_resilient(fully_specified_name, max_retries, request_timeout)
    if not fallbacks:
        return primary
    return cast(
        "BaseChatModel", primary.with_fallbacks([_load_resilient(f, max_retries, request_timeout) for f in fallbacks])
    )
