#!/usr/bin/env python3
"""
Hardened Multi-Agent Observability & Evaluation Trace Analyzer (v2).
Fixes cache-denominator survivorship bias, separates per-span context volume
from cumulative multi-agent trace spend, and detects system-prefix cache busting.
"""

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

LEGACY_MODEL_PATTERNS = (
    "gpt-4o",
    "gpt-4-turbo",
    "gpt-3.5",
    "claude-3-",
    "claude-sonnet-4.5",
    "gemini-1.5",
    "gemini-2.0",
    "gemini-2.5-flash",
)

CACHE_CAPABLE_PATTERNS = (
    "gpt-4o",
    "gpt-5",
    "o1",
    "o3",
    "claude-3",
    "claude-sonnet-4",
    "claude-opus-4",
    "claude-haiku-4",
    "gemini-1.5",
    "gemini-2.",
    "gemini-3",
    "deepseek",
)


def estimate_tokens_from_chars(text: str) -> int:
    return math.ceil(len(text) / 4.0) if text else 0


def percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = (len(sorted_vals) - 1) * pct
    lo, hi = math.floor(idx), math.ceil(idx)
    if lo == hi:
        return float(sorted_vals[int(idx)])
    return float(sorted_vals[lo] * (hi - idx) + sorted_vals[hi] * (idx - lo))


def is_model_cache_capable(model: str, explicit_flag: Optional[bool]) -> bool:
    if explicit_flag is not None:
        return bool(explicit_flag)
    return any(pat in model.lower() for pat in CACHE_CAPABLE_PATTERNS)


def normalize_span(raw: Dict[str, Any]) -> Dict[str, Any]:
    attrs = raw.get("attributes") or raw.get("meta") or {}
    usage = raw.get("usage") or attrs.get("usage") or {}

    op_name = (
        raw.get("operation_name")
        or attrs.get("gen_ai.operation.name")
        or ("chat" if (raw.get("model") or attrs.get("gen_ai.request.model")) else "unknown")
    ).lower()

    trace_id = str(raw.get("trace_id") or raw.get("traceId") or attrs.get("trace_id") or "unknown-trace")
    service = str(raw.get("service_name") or raw.get("service") or attrs.get("service.name") or "default-service")
    provider = str(raw.get("provider") or attrs.get("gen_ai.provider.name") or attrs.get("gen_ai.system") or "unknown").lower()
    model = str(raw.get("model") or attrs.get("gen_ai.request.model") or "unknown").lower()
    agent_name = str(attrs.get("gen_ai.agent.name") or raw.get("agent_name") or service)
    framework = str(raw.get("framework") or attrs.get("gen_ai.framework") or "none")
    prefix_hash = attrs.get("gen_ai.prompt.system_prefix_hash")

    status_code = raw.get("status_code") or attrs.get("http.status_code") or 200
    try:
        status_code = int(status_code)
    except (ValueError, TypeError):
        status_code = 200

    error_flag = bool(raw.get("error") or raw.get("error_type") or status_code >= 400)
    error_bucket: Optional[str] = None
    if error_flag:
        err_str = str(raw.get("error_type") or raw.get("error") or "").lower()
        if status_code == 429 or "rate_limit" in err_str or "resource_exhausted" in err_str:
            error_bucket = "429_rate_limit"
        elif 400 <= status_code < 500:
            error_bucket = "4xx_other"
        elif 500 <= status_code < 600:
            error_bucket = "50x_server"
        else:
            error_bucket = "other"

    role_tokens: Dict[str, int] = defaultdict(int)
    if isinstance(raw.get("role_tokens"), dict):
        for r, cnt in raw["role_tokens"].items():
            role_tokens[str(r).lower()] += int(cnt or 0)
    else:
        messages = raw.get("messages") or attrs.get("gen_ai.input.messages") or []
        if isinstance(messages, list):
            for msg in messages:
                if isinstance(msg, dict):
                    role = str(msg.get("role", "other")).lower()
                    if role not in ("system", "user", "developer", "assistant"):
                        role = "other"
                    content = msg.get("content", "")
                    if not isinstance(content, str):
                        content = json.dumps(content, sort_keys=True)
                    role_tokens[role] += int(msg.get("tokens") or estimate_tokens_from_chars(content))

    input_tokens = int(
        usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or attrs.get("gen_ai.usage.input_tokens")
        or sum(role_tokens.values())
        or 0
    )
    output_tokens = int(
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or attrs.get("gen_ai.usage.output_tokens")
        or 0
    )
    cached_tokens = int(
        usage.get("cached_read_input_tokens")
        or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        or attrs.get("gen_ai.usage.cached_read_input_tokens")
        or 0
    )

    return {
        "op_name": op_name,
        "trace_id": trace_id,
        "service": service,
        "agent_name": agent_name,
        "provider": provider,
        "model": model,
        "framework": framework,
        "prefix_hash": prefix_hash,
        "error_bucket": error_bucket,
        "role_tokens": dict(role_tokens),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_read_input_tokens": cached_tokens,
        "cache_capable": is_model_cache_capable(model, usage.get("cache_capable")),
    }


def analyze_spans(spans: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not spans:
        return {"error": "No spans found. Bootstrap instrumentation using references/zero-to-one-instrumentation.md first."}

    llm_spans = [s for s in spans if s["op_name"] in ("chat", "completion", "generate_content") or s["model"] != "unknown"]
    if not llm_spans:
        llm_spans = spans

    providers = Counter(s["provider"] for s in llm_spans if s["provider"] != "unknown")
    models = Counter(s["model"] for s in llm_spans if s["model"] != "unknown")
    legacy_models = sorted({m for m in models if any(pat in m for pat in LEGACY_MODEL_PATTERNS)})

    total_role_tokens: Counter = Counter()
    for s in llm_spans:
        for role, cnt in s["role_tokens"].items():
            total_role_tokens[role] += cnt
    sum_role_tokens = sum(total_role_tokens.values())
    role_shares = {
        r: round((cnt / sum_role_tokens) * 100, 1) if sum_role_tokens else 0.0
        for r, cnt in total_role_tokens.items()
    }

    # Fixed cache-hit calculation: include all known cache-capable models, not just those that already hit
    cache_eligible = [s for s in llm_spans if s["cache_capable"] or s["cached_read_input_tokens"] > 0]
    cache_hits = sum(1 for s in cache_eligible if s["cached_read_input_tokens"] > 0)
    cache_hit_rate = round((cache_hits / len(cache_eligible)) * 100, 1) if cache_eligible else 0.0

    agent_hashes: Dict[str, List[str]] = defaultdict(list)
    for s in llm_spans:
        if s["prefix_hash"]:
            agent_hashes[s["agent_name"]].append(s["prefix_hash"])
    cache_busting_agents = [
        agent for agent, hashes in agent_hashes.items()
        if len(hashes) >= 3 and len(set(hashes)) / len(hashes) > 0.8
    ]

    span_token_totals = [s["input_tokens"] + s["output_tokens"] for s in llm_spans if (s["input_tokens"] + s["output_tokens"]) > 0]
    trace_tokens: Dict[str, int] = defaultdict(int)
    trace_services: Dict[str, set] = defaultdict(set)
    trace_llm_calls: Counter = Counter()

    for s in spans:
        tid = s["trace_id"]
        trace_services[tid].add(s["service"])
    for s in llm_spans:
        tid = s["trace_id"]
        trace_tokens[tid] += s["input_tokens"] + s["output_tokens"]
        trace_llm_calls[tid] += 1

    p50_span_tokens = round(percentile(span_token_totals, 0.50), 1)
    p90_span_tokens = round(percentile(span_token_totals, 0.90), 1)
    p50_trace_tokens = round(percentile(list(trace_tokens.values()), 0.50), 1)

    error_spans = [s for s in llm_spans if s["error_bucket"] is not None]
    error_counts = Counter(s["error_bucket"] for s in error_spans)
    total_errors = len(error_spans)
    span_error_rate = round((total_errors / len(llm_spans)) * 100, 2) if llm_spans else 0.0
    error_shares = {
        b: round((cnt / total_errors) * 100, 1) if total_errors else 0.0
        for b, cnt in error_counts.items()
    }

    total_traces = len(trace_services)
    s1 = sum(1 for svcs in trace_services.values() if len(svcs) == 1)
    s2 = sum(1 for svcs in trace_services.values() if len(svcs) == 2)
    s3 = sum(1 for svcs in trace_services.values() if len(svcs) >= 3)

    findings = []
    if role_shares.get("system", 0.0) > 50.0 and cache_hit_rate < 28.0:
        findings.append(
            f"HIGH [Prompt Caching]: System role is {role_shares.get('system')}% of input tokens, "
            f"but cache read rate is only {cache_hit_rate}% across {len(cache_eligible)} eligible calls (2026 baseline: 28%)."
        )
    if cache_busting_agents:
        findings.append(
            f"HIGH [Prefix Instability]: Agents {cache_busting_agents} mutate the first 512 chars of their "
            "system prompt on >80% of calls, invalidating provider prefix caches."
        )
    if error_shares.get("429_rate_limit", 0.0) >= 25.0:
        findings.append(
            f"HIGH [Capacity Ceiling]: 429 rate-limit errors account for {error_shares['429_rate_limit']}% of failures. "
            "Enforce per-trace AgentBudget caps and jittered backoff."
        )
    if p50_span_tokens > 5251:
        findings.append(
            f"MEDIUM [Context Bloat]: Median tokens per LLM span ({p50_span_tokens}) exceeds the 2026 baseline (5,251)."
        )

    return {
        "summary": {
            "total_spans": len(spans),
            "llm_spans": len(llm_spans),
            "total_traces": total_traces,
            "distinct_models": len(models),
            "distinct_providers": len(providers),
            "legacy_models_detected": legacy_models,
        },
        "context_and_caching": {
            "role_token_share_pct": role_shares,
            "cache_hit_call_rate_pct": cache_hit_rate,
            "cache_busting_agents": cache_busting_agents,
            "p50_tokens_per_llm_span": p50_span_tokens,
            "p90_tokens_per_llm_span": p90_span_tokens,
            "p50_cumulative_tokens_per_trace": p50_trace_tokens,
            "max_llm_calls_in_single_trace": max(trace_llm_calls.values(), default=0),
        },
        "reliability": {
            "llm_span_error_rate_pct": span_error_rate,
            "error_breakdown_pct": error_shares,
        },
        "topology": {
            "monolithic_1_service_pct": round((s1 / total_traces) * 100, 1) if total_traces else 0.0,
            "two_services_pct": round((s2 / total_traces) * 100, 1) if total_traces else 0.0,
            "distributed_3plus_services_pct": round((s3 / total_traces) * 100, 1) if total_traces else 0.0,
        },
        "findings": findings,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_file", type=Path)
    args = parser.parse_args()
    text = args.trace_file.read_text(encoding="utf-8").strip()
    raw_items = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line.strip()]
    spans = [normalize_span(x) for x in raw_items if isinstance(x, dict)]
    print(json.dumps(analyze_spans(spans), indent=2))


if __name__ == "__main__":
    main()
