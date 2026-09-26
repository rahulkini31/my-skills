# Zero-to-One Multi-Agent Instrumentation Guide

When the target codebase has no observability layer, generate and wire in this vendor-neutral tracing module before running any evaluation scripts.

## Architectural Requirements for Multi-Agent Tracing

1. **Three-Tier Span Hierarchy:**
   - `invoke_agent` (`gen_ai.operation.name = "invoke_agent"`): Wraps each autonomous agent execution, recording `gen_ai.agent.name`, `gen_ai.agent.id`, loop iteration count, and handoff targets.
   - `chat` (`gen_ai.operation.name = "chat"`): Child span wrapping every model provider call, capturing `gen_ai.provider.name`, `gen_ai.request.model`, token usage (`input_tokens`, `output_tokens`, `cached_read_input_tokens`), and privacy-safe role token shares (`char_count / 4`).
   - `execute_tool` (`gen_ai.operation.name = "execute_tool"`): Sibling/child span wrapping every tool or MCP call, recording `gen_ai.tool.name`, `gen_ai.tool.call.id`, latency, and exceptions.
2. **Cross-Agent & Cross-Service Context Propagation:**
   - Use Python `contextvars` (or async context propagation in TypeScript/Go) for in-process async/thread handoffs.
   - Inject/extract W3C `traceparent` (`00-<trace_id>-<span_id>-01`) and `x-agent-budget-remaining` headers across HTTP, gRPC, or queue boundaries so distributed multi-agent calls assemble into a single trace tree.
3. **Privacy-Safe Payload Capture (Opt-In Content):**
   - By default, compute role character lengths (`ceil(len(content) / 4)`) and prefix hashes (first 256 chars of `system` prompt) in memory without writing raw PII prompts to disk unless `OTEL_GENAI_CAPTURE_CONTENT=true` is explicitly set.

## Drop-In Python Instrumentation Module (`observability/tracer.py`)

If the project lacks tracing, create `observability/tracer.py` (adapt to TypeScript/Go if the target repo is non-Python) and wrap the entrypoints:

```python
import contextvars
import functools
import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_trace_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("trace_id", default=None)
_span_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("span_id", default=None)

TRACE_LOG_PATH = Path(os.getenv("AGENT_TRACE_LOG_PATH", "traces/agent_spans.jsonl"))
CAPTURE_RAW_CONTENT = os.getenv("OTEL_GENAI_CAPTURE_CONTENT", "false").lower() == "true"
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "multi-agent-orchestrator")


def _emit_span(span_payload: Dict[str, Any]) -> None:
    TRACE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TRACE_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(span_payload) + "\n")


def inject_trace_headers(headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Injects W3C traceparent for cross-service multi-agent HTTP/MCP calls."""
    out = dict(headers or {})
    tid = _trace_id_var.get() or uuid.uuid4().hex
    sid = _span_id_var.get() or uuid.uuid4().hex[:16]
    out["traceparent"] = f"00-{tid}-{sid}-01"
    return out


def extract_trace_headers(headers: Dict[str, str]) -> None:
    """Extracts W3C traceparent on downstream agent services."""
    tp = headers.get("traceparent") or headers.get("Traceparent")
    if tp and tp.count("-") >= 3:
        _, tid, parent_sid, _ = tp.split("-", 3)
        _trace_id_var.set(tid)
        _span_id_var.set(parent_sid)


def summarize_messages_privacy_safe(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Computes role token proxies and system prefix hash without storing raw PII."""
    role_tokens: Dict[str, int] = {"system": 0, "user": 0, "developer": 0, "assistant": 0, "other": 0}
    system_prefix_hash = None
    for idx, msg in enumerate(messages or []):
        role = str(msg.get("role", "other")).lower()
        if role not in role_tokens:
            role = "other"
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, sort_keys=True)
        role_tokens[role] += math.ceil(len(content) / 4.0)
        if idx == 0 and role == "system" and content:
            # Hash the first 512 chars of the system prompt to detect prefix cache invalidation
            system_prefix_hash = hashlib.sha256(content[:512].encode("utf-8")).hexdigest()[:12]
    return {"role_tokens": role_tokens, "system_prefix_hash": system_prefix_hash}


def trace_agent(agent_name: str, agent_id: Optional[str] = None, framework: str = "custom"):
    """Decorator for autonomous agent entrypoints (sync or async)."""
    def decorator(func: Callable):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            tid = _trace_id_var.get() or uuid.uuid4().hex
            parent_sid = _span_id_var.get()
            sid = uuid.uuid4().hex[:16]
            t_tok = _trace_id_var.set(tid)
            s_tok = _span_id_var.set(sid)
            start = time.perf_counter()
            status_code, err_type = 200, None
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                status_code = getattr(exc, "status_code", 500)
                err_type = type(exc).__name__
                raise
            finally:
                _emit_span({
                    "trace_id": tid,
                    "span_id": sid,
                    "parent_span_id": parent_sid,
                    "service_name": SERVICE_NAME,
                    "framework": framework,
                    "latency_ms": round((time.perf_counter() - start) * 1000, 2),
                    "status_code": status_code,
                    "error_type": err_type,
                    "attributes": {
                        "gen_ai.operation.name": "invoke_agent",
                        "gen_ai.agent.name": agent_name,
                        "gen_ai.agent.id": agent_id or agent_name,
                    },
                })
                _trace_id_var.reset(t_tok)
                _span_id_var.reset(s_tok)
        return async_wrapper
    return decorator


def record_llm_span(
    provider: str,
    model: str,
    messages: List[Dict[str, Any]],
    input_tokens: Optional[int] = None,
    output_tokens: int = 0,
    cached_read_input_tokens: int = 0,
    cache_capable: bool = True,
    latency_ms: float = 0.0,
    status_code: int = 200,
    error_type: Optional[str] = None,
    agent_name: str = "unknown-agent",
) -> None:
    """Call inside your LLM gateway/wrapper after each model invocation."""
    tid = _trace_id_var.get() or uuid.uuid4().hex
    parent_sid = _span_id_var.get()
    sid = uuid.uuid4().hex[:16]
    msg_meta = summarize_messages_privacy_safe(messages)
    est_input = sum(msg_meta["role_tokens"].values())

    span = {
        "trace_id": tid,
        "span_id": sid,
        "parent_span_id": parent_sid,
        "service_name": SERVICE_NAME,
        "provider": provider,
        "model": model,
        "latency_ms": round(latency_ms, 2),
        "status_code": status_code,
        "error_type": error_type,
        "role_tokens": msg_meta["role_tokens"],
        "attributes": {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": provider,
            "gen_ai.request.model": model,
            "gen_ai.agent.name": agent_name,
            "gen_ai.prompt.system_prefix_hash": msg_meta["system_prefix_hash"],
        },
        "usage": {
            "input_tokens": input_tokens if input_tokens is not None else est_input,
            "output_tokens": output_tokens,
            "cached_read_input_tokens": cached_read_input_tokens,
            "cache_capable": cache_capable,
        },
    }
    if CAPTURE_RAW_CONTENT:
        span["messages"] = messages
    _emit_span(span)