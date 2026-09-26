# Zero-to-One Multi-Agent Instrumentation Guide

When the target codebase has no observability layer, generate and wire in this vendor-neutral tracing module before running any evaluation scripts.

## Architectural Requirements for Multi-Agent Tracing

1. **Three-Tier Span Hierarchy:**
   - `invoke_agent` (`gen_ai.operation.name = "invoke_agent"`): Wraps each autonomous agent execution, recording `gen_ai.agent.name`, `gen_ai.agent.id`, and active `AgentBudget` limits.
   - `chat` (`gen_ai.operation.name = "chat"`): Child span wrapping every model call, capturing `gen_ai.provider.name`, `gen_ai.request.model`, token usage (`input_tokens`, `output_tokens`, `cached_read_input_tokens`), and privacy-safe role token shares (`ceil(char_count / 4)`).
   - `execute_tool` (`gen_ai.operation.name = "execute_tool"`): Child span wrapping every tool or MCP invocation, recording `gen_ai.tool.name`, `gen_ai.tool.call.id`, latency, and exceptions.
2. **Cross-Agent & Cross-Service Context Propagation:**
   - Use `contextvars` for in-process async/thread handoffs.
   - Inject and extract W3C `traceparent` (`00-<32-hex-trace-id>-<16-hex-span-id>-01`) AND `x-agent-budget-remaining` (JSON-serialized remaining call, input token, cost, and deadline budgets) across HTTP, gRPC, queue, and MCP boundaries.
3. **Privacy-Safe Payload Capture (512-Char Prefix Hash):**
   - Compute role token proxies (`ceil(len(content) / 4)`) and the SHA-256 hash of the first 512 characters of the `system` prompt in memory. Never persist raw prompt strings unless `OTEL_GENAI_CAPTURE_CONTENT=true` is explicitly enabled.

## Drop-In Python Instrumentation Module (`observability/tracer.py`)

If the project lacks tracing, create `observability/tracer.py` (adapt to TypeScript/Go if the target repo is non-Python) and wrap the entrypoints:

```python
import contextvars
import functools
import hashlib
import inspect
import json
import math
import os
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


@dataclass
class AgentBudget:
    """Enforces per-trace call, token, cost, and wall-clock budgets across multi-agent loops."""
    max_calls: int = 8
    max_input_tokens: int = 32000
    max_cost_usd: float = 0.50
    max_wall_time_ms: float = 60000.0
    calls_used: int = 0
    input_tokens_used: int = 0
    cost_used: float = 0.0
    start_epoch_ms: float = 0.0

    def __post_init__(self) -> None:
        if self.start_epoch_ms <= 0.0:
            self.start_epoch_ms = time.time() * 1000.0

    @property
    def remaining_calls(self) -> int:
        return max(0, self.max_calls - self.calls_used)

    @property
    def remaining_input_tokens(self) -> int:
        return max(0, self.max_input_tokens - self.input_tokens_used)

    @property
    def remaining_cost_usd(self) -> float:
        return round(max(0.0, self.max_cost_usd - self.cost_used), 6)

    @property
    def remaining_wall_time_ms(self) -> float:
        elapsed = (time.time() * 1000.0) - self.start_epoch_ms
        return round(max(0.0, self.max_wall_time_ms - elapsed), 2)

    def record_and_check(self, input_tokens: int = 0, cost_usd: float = 0.0) -> None:
        self.calls_used += 1
        self.input_tokens_used += input_tokens
        self.cost_used += cost_usd
        if self.calls_used > self.max_calls:
            raise RuntimeError(f"AgentBudgetExceeded: max_calls ({self.max_calls}) reached")
        if self.input_tokens_used > self.max_input_tokens:
            raise RuntimeError(f"AgentBudgetExceeded: max_input_tokens ({self.max_input_tokens}) reached")
        if self.cost_used > self.max_cost_usd:
            raise RuntimeError(f"AgentBudgetExceeded: max_cost_usd ({self.max_cost_usd}) reached")
        if self.remaining_wall_time_ms <= 0.0:
            raise RuntimeError(f"AgentBudgetExceeded: max_wall_time_ms ({self.max_wall_time_ms}ms) reached")

    def to_header_json(self) -> str:
        return json.dumps({
            "remaining_calls": self.remaining_calls,
            "remaining_input_tokens": self.remaining_input_tokens,
            "remaining_cost_usd": self.remaining_cost_usd,
            "remaining_wall_time_ms": self.remaining_wall_time_ms,
        })

    @classmethod
    def from_header_json(cls, header_val: str) -> "AgentBudget":
        data = json.loads(header_val)
        return cls(
            max_calls=int(data.get("remaining_calls", 8)),
            max_input_tokens=int(data.get("remaining_input_tokens", 32000)),
            max_cost_usd=float(data.get("remaining_cost_usd", 0.50)),
            max_wall_time_ms=float(data.get("remaining_wall_time_ms", 60000.0)),
        )


_trace_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("trace_id", default=None)
_span_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("span_id", default=None)
_budget_var: contextvars.ContextVar[Optional[AgentBudget]] = contextvars.ContextVar("agent_budget", default=None)

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "multi-agent-orchestrator")


def _to_otlp_http_payload(span_payload: Dict[str, Any], start_ns: int, end_ns: int) -> Dict[str, Any]:
    """Converts a normalized span dictionary into an OTLP/HTTP JSON ExportTraceServiceRequest."""
    otlp_attrs = []
    merged_attrs = dict(span_payload.get("attributes") or {})
    for k, v in (span_payload.get("usage") or {}).items():
        merged_attrs[f"gen_ai.usage.{k}"] = v

    for key, val in merged_attrs.items():
        if val is None:
            continue
        if isinstance(val, bool):
            otlp_attrs.append({"key": str(key), "value": {"boolValue": val}})
        elif isinstance(val, int):
            otlp_attrs.append({"key": str(key), "value": {"intValue": str(val)}})
        elif isinstance(val, float):
            otlp_attrs.append({"key": str(key), "value": {"doubleValue": val}})
        else:
            otlp_attrs.append({"key": str(key), "value": {"stringValue": str(val)}})

    status_code = int(span_payload.get("status_code", 200))
    otlp_span: Dict[str, Any] = {
        "traceId": span_payload["trace_id"],
        "spanId": span_payload["span_id"],
        "name": merged_attrs.get("gen_ai.operation.name", "gen_ai.step"),
        "kind": 1,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": otlp_attrs,
        "status": {"code": 2 if status_code >= 400 else 1},
    }
    if span_payload.get("parent_span_id"):
        otlp_span["parentSpanId"] = span_payload["parent_span_id"]

    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": span_payload.get("service_name", SERVICE_NAME)}}
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "ai-observability-eval.tracer", "version": "1.0.0"},
                        "spans": [otlp_span],
                    }
                ],
            }
        ]
    }


def _emit_span(span_payload: Dict[str, Any], start_ns: Optional[int] = None, end_ns: Optional[int] = None) -> None:
    """Dual-sink emitter: appends JSONL locally and POSTs OTLP/HTTP JSON when configured."""
    log_path = Path(os.getenv("AGENT_TRACE_LOG_PATH", "traces/baseline_spans.jsonl"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(span_payload) + "\n")

    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if otlp_endpoint:
        now_ns = time.time_ns()
        s_ns = start_ns or (now_ns - int(float(span_payload.get("latency_ms", 1.0)) * 1_000_000))
        e_ns = end_ns or now_ns
        otlp_body = _to_otlp_http_payload(span_payload, s_ns, e_ns)
        try:
            req = urllib.request.Request(
                otlp_endpoint,
                data=json.dumps(otlp_body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=0.5)
        except Exception:
            pass


def inject_trace_headers(headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Injects W3C traceparent and x-agent-budget-remaining for cross-service HTTP/gRPC/MCP calls."""
    out = dict(headers or {})
    tid = _trace_id_var.get() or uuid.uuid4().hex
    sid = _span_id_var.get() or uuid.uuid4().hex[:16]
    budget = _budget_var.get() or AgentBudget()
    out["traceparent"] = f"00-{tid}-{sid}-01"
    out["x-agent-budget-remaining"] = budget.to_header_json()
    return out


def extract_trace_headers(headers: Dict[str, str]) -> AgentBudget:
    """Extracts W3C traceparent and x-agent-budget-remaining on downstream agent services."""
    tp = headers.get("traceparent") or headers.get("Traceparent")
    if tp and tp.count("-") >= 3:
        _, tid, parent_sid, _ = tp.split("-", 3)
        _trace_id_var.set(tid)
        _span_id_var.set(parent_sid)
    budget_hdr = headers.get("x-agent-budget-remaining") or headers.get("X-Agent-Budget-Remaining")
    budget = AgentBudget.from_header_json(budget_hdr) if budget_hdr else AgentBudget()
    _budget_var.set(budget)
    return budget


def summarize_messages_privacy_safe(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Computes role token proxies and the first-512-char system prefix hash without storing raw PII."""
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
            system_prefix_hash = hashlib.sha256(content[:512].encode("utf-8")).hexdigest()[:12]
    return {"role_tokens": role_tokens, "system_prefix_hash": system_prefix_hash}


def trace_agent(
    agent_name: str,
    agent_id: Optional[str] = None,
    framework: str = "custom",
    max_calls: int = 8,
    max_input_tokens: int = 32000,
):
    """Decorator for both synchronous and asynchronous autonomous agent entrypoints."""
    def decorator(func: Callable):
        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                tid = _trace_id_var.get() or uuid.uuid4().hex
                parent_sid = _span_id_var.get()
                sid = uuid.uuid4().hex[:16]
                budget = _budget_var.get() or AgentBudget(max_calls=max_calls, max_input_tokens=max_input_tokens)
                t_tok = _trace_id_var.set(tid)
                s_tok = _span_id_var.set(sid)
                b_tok = _budget_var.set(budget)
                start_ns = time.time_ns()
                status_code, err_type = 200, None
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    status_code = getattr(exc, "status_code", 500)
                    err_type = type(exc).__name__
                    raise
                finally:
                    end_ns = time.time_ns()
                    _emit_span({
                        "trace_id": tid,
                        "span_id": sid,
                        "parent_span_id": parent_sid,
                        "service_name": SERVICE_NAME,
                        "framework": framework,
                        "latency_ms": round((end_ns - start_ns) / 1_000_000, 2),
                        "status_code": status_code,
                        "error_type": err_type,
                        "attributes": {
                            "gen_ai.operation.name": "invoke_agent",
                            "gen_ai.agent.name": agent_name,
                            "gen_ai.agent.id": agent_id or agent_name,
                            "gen_ai.budget.remaining_calls": budget.remaining_calls,
                            "gen_ai.budget.remaining_input_tokens": budget.remaining_input_tokens,
                        },
                    }, start_ns, end_ns)
                    _budget_var.reset(b_tok)
                    _span_id_var.reset(s_tok)
                    _trace_id_var.reset(t_tok)
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                tid = _trace_id_var.get() or uuid.uuid4().hex
                parent_sid = _span_id_var.get()
                sid = uuid.uuid4().hex[:16]
                budget = _budget_var.get() or AgentBudget(max_calls=max_calls, max_input_tokens=max_input_tokens)
                t_tok = _trace_id_var.set(tid)
                s_tok = _span_id_var.set(sid)
                b_tok = _budget_var.set(budget)
                start_ns = time.time_ns()
                status_code, err_type = 200, None
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    status_code = getattr(exc, "status_code", 500)
                    err_type = type(exc).__name__
                    raise
                finally:
                    end_ns = time.time_ns()
                    _emit_span({
                        "trace_id": tid,
                        "span_id": sid,
                        "parent_span_id": parent_sid,
                        "service_name": SERVICE_NAME,
                        "framework": framework,
                        "latency_ms": round((end_ns - start_ns) / 1_000_000, 2),
                        "status_code": status_code,
                        "error_type": err_type,
                        "attributes": {
                            "gen_ai.operation.name": "invoke_agent",
                            "gen_ai.agent.name": agent_name,
                            "gen_ai.agent.id": agent_id or agent_name,
                            "gen_ai.budget.remaining_calls": budget.remaining_calls,
                            "gen_ai.budget.remaining_input_tokens": budget.remaining_input_tokens,
                        },
                    }, start_ns, end_ns)
                    _budget_var.reset(b_tok)
                    _span_id_var.reset(s_tok)
                    _trace_id_var.reset(t_tok)
            return sync_wrapper
    return decorator


def trace_tool(tool_name: str):
    """Decorator for both synchronous and asynchronous tool / MCP executions (emits execute_tool spans)."""
    def decorator(func: Callable):
        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                tid = _trace_id_var.get() or uuid.uuid4().hex
                parent_sid = _span_id_var.get()
                sid = uuid.uuid4().hex[:16]
                s_tok = _span_id_var.set(sid)
                start_ns = time.time_ns()
                status_code, err_type = 200, None
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    status_code = getattr(exc, "status_code", 500)
                    err_type = type(exc).__name__
                    raise
                finally:
                    end_ns = time.time_ns()
                    _emit_span({
                        "trace_id": tid,
                        "span_id": sid,
                        "parent_span_id": parent_sid,
                        "service_name": SERVICE_NAME,
                        "latency_ms": round((end_ns - start_ns) / 1_000_000, 2),
                        "status_code": status_code,
                        "error_type": err_type,
                        "attributes": {
                            "gen_ai.operation.name": "execute_tool",
                            "gen_ai.tool.name": tool_name,
                            "gen_ai.tool.call.id": sid,
                        },
                    }, start_ns, end_ns)
                    _span_id_var.reset(s_tok)
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                tid = _trace_id_var.get() or uuid.uuid4().hex
                parent_sid = _span_id_var.get()
                sid = uuid.uuid4().hex[:16]
                s_tok = _span_id_var.set(sid)
                start_ns = time.time_ns()
                status_code, err_type = 200, None
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    status_code = getattr(exc, "status_code", 500)
                    err_type = type(exc).__name__
                    raise
                finally:
                    end_ns = time.time_ns()
                    _emit_span({
                        "trace_id": tid,
                        "span_id": sid,
                        "parent_span_id": parent_sid,
                        "service_name": SERVICE_NAME,
                        "latency_ms": round((end_ns - start_ns) / 1_000_000, 2),
                        "status_code": status_code,
                        "error_type": err_type,
                        "attributes": {
                            "gen_ai.operation.name": "execute_tool",
                            "gen_ai.tool.name": tool_name,
                            "gen_ai.tool.call.id": sid,
                        },
                    }, start_ns, end_ns)
                    _span_id_var.reset(s_tok)
            return sync_wrapper
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
    cost_usd: float = 0.0,
) -> None:
    """Records an LLM chat span and enforces the active AgentBudget."""
    tid = _trace_id_var.get() or uuid.uuid4().hex
    parent_sid = _span_id_var.get()
    sid = uuid.uuid4().hex[:16]
    msg_meta = summarize_messages_privacy_safe(messages)
    resolved_input = int(input_tokens if input_tokens is not None else sum(msg_meta["role_tokens"].values()))

    budget = _budget_var.get()
    if budget is not None:
        budget.record_and_check(input_tokens=resolved_input, cost_usd=cost_usd)

    span: Dict[str, Any] = {
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
            "input_tokens": resolved_input,
            "output_tokens": int(output_tokens),
            "cached_read_input_tokens": int(cached_read_input_tokens),
            "cache_capable": cache_capable,
        },
    }
    if os.getenv("OTEL_GENAI_CAPTURE_CONTENT", "false").lower() == "true":
        span["messages"] = messages
    _emit_span(span)
```