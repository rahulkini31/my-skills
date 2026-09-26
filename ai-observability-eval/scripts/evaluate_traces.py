#!/usr/bin/env python3
"""
Hardened Multi-Agent Observability & Evaluation Trace Analyzer (v3).
Audits the 7 production benchmark dimensions from the Datadog 2026 State of AI Engineering Report
PLUS strict trace-graph integrity, W3C traceparent propagation, token metadata completeness,
and PII / raw prompt content leak detection.
"""

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

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

W3C_TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
EMAIL_PII_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
SECRET_KEY_RE = re.compile(r"\b(?:sk-[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9\-._~+/]+=*)\b")


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


def scan_for_pii(raw: Dict[str, Any]) -> List[str]:
    """Scans serialized span payload for email addresses and API secrets."""
    serialized = json.dumps(raw, ensure_ascii=False)
    hits: List[str] = []
    if EMAIL_PII_RE.search(serialized):
        hits.append("email_address")
    if SECRET_KEY_RE.search(serialized):
        hits.append("api_key_or_bearer_token")
    return hits


def normalize_span(raw: Dict[str, Any], index: int = 0) -> Dict[str, Any]:
    attrs = raw.get("attributes") or raw.get("meta") or {}
    usage = raw.get("usage") or attrs.get("usage") or {}

    raw_trace_id = raw.get("trace_id") or raw.get("traceId") or attrs.get("trace_id")
    raw_span_id = raw.get("span_id") or raw.get("spanId") or attrs.get("span_id")
    parent_span_id = raw.get("parent_span_id") or raw.get("parentSpanId") or attrs.get("parent_span_id")

    trace_id = str(raw_trace_id) if raw_trace_id else "unknown-trace"
    span_id = str(raw_span_id) if raw_span_id else f"missing-span-id-{index}"

    op_name = str(
        raw.get("operation_name")
        or attrs.get("gen_ai.operation.name")
        or ("chat" if (raw.get("model") or attrs.get("gen_ai.request.model")) else "unknown")
    ).lower()

    service = str(raw.get("service_name") or raw.get("service") or attrs.get("service.name") or "default-service")
    provider = str(raw.get("provider") or attrs.get("gen_ai.provider.name") or attrs.get("gen_ai.system") or "unknown").lower()
    model = str(raw.get("model") or attrs.get("gen_ai.request.model") or "unknown").lower()
    agent_name = str(attrs.get("gen_ai.agent.name") or raw.get("agent_name") or service)
    framework = str(raw.get("framework") or attrs.get("gen_ai.framework") or "none")
    prefix_hash = attrs.get("gen_ai.prompt.system_prefix_hash")

    # Check W3C traceparent header if recorded
    traceparent = raw.get("traceparent") or attrs.get("traceparent") or attrs.get("http.request.header.traceparent")
    traceparent_valid = True
    if traceparent is not None:
        traceparent_valid = bool(W3C_TRACEPARENT_RE.match(str(traceparent).strip()))

    # Check if x-agent-budget-remaining is present when traceparent is used across services
    budget_header = (
        raw.get("x_agent_budget_remaining")
        or attrs.get("x-agent-budget-remaining")
        or attrs.get("http.request.header.x_agent_budget_remaining")
    )

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

    messages = raw.get("messages") or attrs.get("gen_ai.input.messages") or attrs.get("gen_ai.prompt") or []
    has_raw_messages = bool(messages) or bool(raw.get("prompt")) or bool(raw.get("completion"))

    role_tokens: Dict[str, int] = defaultdict(int)
    if isinstance(raw.get("role_tokens"), dict):
        for r, cnt in raw["role_tokens"].items():
            role_tokens[str(r).lower()] += int(cnt or 0)
    elif isinstance(messages, list):
        for msg in messages:
            if isinstance(msg, dict):
                role = str(msg.get("role", "other")).lower()
                if role not in ("system", "user", "developer", "assistant"):
                    role = "other"
                content = msg.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, sort_keys=True)
                role_tokens[role] += int(msg.get("tokens") or estimate_tokens_from_chars(content))

    explicit_input = (
        usage.get("input_tokens")
        if usage.get("input_tokens") is not None
        else (usage.get("prompt_tokens") if usage.get("prompt_tokens") is not None else attrs.get("gen_ai.usage.input_tokens"))
    )
    explicit_output = (
        usage.get("output_tokens")
        if usage.get("output_tokens") is not None
        else (usage.get("completion_tokens") if usage.get("completion_tokens") is not None else attrs.get("gen_ai.usage.output_tokens"))
    )
    has_explicit_token_metadata = explicit_input is not None and explicit_output is not None

    input_tokens = int(explicit_input if explicit_input is not None else sum(role_tokens.values()))
    output_tokens = int(explicit_output or 0)
    cached_tokens = int(
        usage.get("cached_read_input_tokens")
        or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        or attrs.get("gen_ai.usage.cached_read_input_tokens")
        or 0
    )

    return {
        "op_name": op_name,
        "trace_id": trace_id,
        "has_valid_trace_id": raw_trace_id is not None and str(raw_trace_id) != "unknown-trace",
        "span_id": span_id,
        "has_valid_span_id": raw_span_id is not None,
        "parent_span_id": str(parent_span_id) if parent_span_id else None,
        "traceparent": traceparent,
        "traceparent_valid": traceparent_valid,
        "has_budget_header": budget_header is not None,
        "service": service,
        "agent_name": agent_name,
        "provider": provider,
        "model": model,
        "framework": framework,
        "prefix_hash": prefix_hash,
        "error_bucket": error_bucket,
        "role_tokens": dict(role_tokens),
        "has_explicit_token_metadata": has_explicit_token_metadata,
        "has_raw_messages": has_raw_messages,
        "pii_detected": scan_for_pii(raw),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_read_input_tokens": cached_tokens,
        "cache_capable": is_model_cache_capable(model, usage.get("cache_capable")),
    }


def analyze_spans(spans: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not spans:
        return {"error": "No spans found. Bootstrap instrumentation using references/zero-to-one-instrumentation.md first."}

    llm_spans = [
        s for s in spans
        if s["op_name"] in ("chat", "completion", "generate_content") or s["model"] != "unknown"
    ]

    # --- Trace Graph Integrity, Propagation & Privacy Checks ---
    trace_span_ids: Dict[str, Set[str]] = defaultdict(set)
    trace_spans_map: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for s in spans:
        if s["has_valid_span_id"]:
            trace_span_ids[s["trace_id"]].add(s["span_id"])
        trace_spans_map[s["trace_id"]].append(s)

    orphaned_span_ids: List[str] = []
    broken_propagation_Ids: List[str] = []
    missing_token_metadata_ids: List[str] = []
    raw_content_span_ids: List[str] = []
    pii_leaking_spans: List[Dict[str, Any]] = []

    for s in spans:
        sid = s["span_id"]
        tid = s["trace_id"]
        pid = s["parent_span_id"]

        # 1. Orphaned span detection:
        #    (a) parent_span_id points to a span_id that does not exist in this trace
        #    (b) missing trace_id or span_id
        #    (c) child operation (chat / execute_tool / unknown) has no parent_span_id (unparented root)
        if not s["has_valid_trace_id"] or not s["has_valid_span_id"]:
            orphaned_span_ids.append(sid)
        elif pid is not None and pid not in trace_span_ids[tid]:
            orphaned_span_ids.append(sid)
        elif pid is None and s["op_name"] in ("chat", "execute_tool", "completion", "generate_content", "unknown"):
            orphaned_span_ids.append(sid)

        # 2. Broken traceparent / propagation detection:
        if not s["traceparent_valid"] or not s["has_valid_trace_id"]:
            broken_propagation_Ids.append(sid)

        # 3. Raw prompt content & PII leak detection:
        if s["has_raw_messages"]:
            raw_content_span_ids.append(sid)
        if s["pii_detected"]:
            pii_leaking_spans.append({"span_id": sid, "pii_types": s["pii_detected"]})

    # Check multi-root or broken cross-service traces
    for tid, t_spans in trace_spans_map.items():
        roots = [s for s in t_spans if s["parent_span_id"] is None]
        services_in_trace = {s["service"] for s in t_spans}
        if len(t_spans) > 1 and len(roots) > 1:
            for r in roots[1:]:
                if r["span_id"] not in orphaned_span_ids:
                    orphaned_span_ids.append(r["span_id"])
        if len(services_in_trace) > 1 and len(roots) > 1:
            for r in roots[1:]:
                if r["span_id"] not in broken_propagation_Ids:
                    broken_propagation_Ids.append(r["span_id"])

    # 4. Missing token metadata on LLM spans (or spans with unknown operation lacking usage)
    target_token_check_spans = llm_spans if llm_spans else spans
    for s in target_token_check_spans:
        if not s["has_explicit_token_metadata"] or (s["input_tokens"] <= 0 and s["output_tokens"] <= 0):
            missing_token_metadata_ids.append(s["span_id"])

    # --- Standard 7 Benchmark Dimensions ---
    eval_llm_spans = llm_spans if llm_spans else spans
    providers = Counter(s["provider"] for s in eval_llm_spans if s["provider"] != "unknown")
    models = Counter(s["model"] for s in eval_llm_spans if s["model"] != "unknown")
    legacy_models = sorted({m for m in models if any(pat in m for pat in LEGACY_MODEL_PATTERNS)})

    total_role_tokens: Counter = Counter()
    for s in eval_llm_spans:
        for role, cnt in s["role_tokens"].items():
            total_role_tokens[role] += cnt
    sum_role_tokens = sum(total_role_tokens.values())
    role_shares = {
        r: round((cnt / sum_role_tokens) * 100, 1) if sum_role_tokens else 0.0
        for r, cnt in total_role_tokens.items()
    }

    cache_eligible = [s for s in eval_llm_spans if s["cache_capable"] or s["cached_read_input_tokens"] > 0]
    cache_hits = sum(1 for s in cache_eligible if s["cached_read_input_tokens"] > 0)
    cache_hit_rate = round((cache_hits / len(cache_eligible)) * 100, 1) if cache_eligible else 0.0

    agent_hashes: Dict[str, List[str]] = defaultdict(list)
    for s in eval_llm_spans:
        if s["prefix_hash"]:
            agent_hashes[s["agent_name"]].append(s["prefix_hash"])
    cache_busting_agents = [
        agent for agent, hashes in agent_hashes.items()
        if len(hashes) >= 3 and len(set(hashes)) / len(hashes) > 0.8
    ]

    span_token_totals = [
        s["input_tokens"] + s["output_tokens"]
        for s in eval_llm_spans
        if (s["input_tokens"] + s["output_tokens"]) > 0
    ]
    trace_tokens: Dict[str, int] = defaultdict(int)
    trace_services: Dict[str, Set[str]] = defaultdict(set)
    trace_llm_calls: Counter = Counter()

    for s in spans:
        trace_services[s["trace_id"]].add(s["service"])
    for s in eval_llm_spans:
        trace_tokens[s["trace_id"]] += s["input_tokens"] + s["output_tokens"]
        trace_llm_calls[s["trace_id"]] += 1

    p50_span_tokens = round(percentile(span_token_totals, 0.50), 1)
    p90_span_tokens = round(percentile(span_token_totals, 0.90), 1)
    p50_trace_tokens = round(percentile(list(trace_tokens.values()), 0.50), 1)

    error_spans = [s for s in eval_llm_spans if s["error_bucket"] is not None]
    error_counts = Counter(s["error_bucket"] for s in error_spans)
    total_errors = len(error_spans)
    span_error_rate = round((total_errors / len(eval_llm_spans)) * 100, 2) if eval_llm_spans else 0.0
    error_shares = {
        b: round((cnt / total_errors) * 100, 1) if total_errors else 0.0
        for b, cnt in error_counts.items()
    }

    total_traces = len(trace_services)
    s1 = sum(1 for svcs in trace_services.values() if len(svcs) == 1)
    s2 = sum(1 for svcs in trace_services.values() if len(svcs) == 2)
    s3 = sum(1 for svcs in trace_services.values() if len(svcs) >= 3)

    # --- Prioritized Findings ---
    findings: List[str] = []
    if orphaned_span_ids:
        findings.append(
            f"HIGH [Trace Integrity - Orphaned Spans]: {len(orphaned_span_ids)} span(s) ({', '.join(orphaned_span_ids[:5])}) "
            "are missing a valid parent span in their trace or lack an invoke_agent root span."
        )
    if broken_propagation_Ids:
        findings.append(
            f"HIGH [Broken Trace Propagation]: {len(broken_propagation_Ids)} span(s) ({', '.join(broken_propagation_Ids[:5])}) "
            "have invalid W3C traceparent headers, missing trace_id, or disconnected cross-service roots."
        )
    if pii_leaking_spans:
        pii_summary = ", ".join(f"{item['span_id']} ({'/'.join(item['pii_types'])})" for item in pii_leaking_spans[:5])
        findings.append(
            f"HIGH [Privacy & PII Violation]: Unredacted PII or credentials detected in {len(pii_leaking_spans)} span(s): {pii_summary}. "
            "Disable raw content capture or scrub PII before emitting spans."
        )
    if raw_content_span_ids:
        findings.append(
            f"MEDIUM [Raw Prompt Content Persisted]: {len(raw_content_span_ids)} span(s) persist raw prompt/message payloads. "
            "Verify OTEL_GENAI_CAPTURE_CONTENT=true was explicitly approved by the author."
        )
    if missing_token_metadata_ids:
        findings.append(
            f"MEDIUM [Missing Token Metadata]: {len(missing_token_metadata_ids)} span(s) ({', '.join(missing_token_metadata_ids[:5])}) "
            "lack explicit usage.input_tokens / usage.output_tokens metadata."
        )
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
    if legacy_models:
        findings.append(
            f"LOW [Model Tech Debt]: Legacy models detected ({', '.join(legacy_models)}). Verify deprecation schedules."
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
        "trace_integrity_and_privacy": {
            "orphaned_span_ids": orphaned_span_ids,
            "broken_propagation_span_ids": broken_propagation_Ids,
            "missing_token_metadata_span_ids": missing_token_metadata_ids,
            "raw_content_span_ids": raw_content_span_ids,
            "pii_leaking_spans": pii_leaking_spans,
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


def format_markdown(report: Dict[str, Any]) -> str:
    if "error" in report:
        return f"**Error:** {report['error']}"
    s = report["summary"]
    ti = report["trace_integrity_and_privacy"]
    c = report["context_and_caching"]
    r = report["reliability"]
    t = report["topology"]

    lines = [
        "## AI Observability & Evaluation Audit Report",
        "",
        "| Metric | Measured Value | 2026 Production Baseline / Target |",
        "| :--- | :--- | :--- |",
        f"| **Distinct Models Used** | {s['distinct_models']} ({s['distinct_providers']} providers) | 70% of orgs use 3+ models; 41% use 6+ |",
        f"| **Orphaned / Broken Propagation Spans** | {len(ti['orphaned_span_ids'])} orphaned / {len(ti['broken_propagation_span_ids'])} broken | 0 orphaned / 100% valid W3C `traceparent` |",
        f"| **PII Leaks / Missing Token Metadata** | {len(ti['pii_leaking_spans'])} PII / {len(ti['missing_token_metadata_span_ids'])} missing usage | 0 PII leaks / 100% token metadata coverage |",
        f"| **System Prompt Token Share** | {c['role_token_share_pct'].get('system', 0.0)}% | 69% system / 28% user / 2% developer |",
        f"| **Prompt Cache Read Rate** | {c['cache_hit_call_rate_pct']}% | 28% baseline (>=70% target on scaffolded agents) |",
        f"| **Median (P50) Tokens / LLM Span** | {c['p50_tokens_per_llm_span']} (P90: {c['p90_tokens_per_llm_span']}) | 5,251 tokens median (doubled YoY) |",
        f"| **Overall LLM Span Error Rate** | {r['llm_span_error_rate_pct']}% | 2% - 5% of all LLM spans |",
        f"| **429 Rate-Limit Error Share** | {r['error_breakdown_pct'].get('429_rate_limit', 0.0)}% | 30% - 60% of all LLM call errors |",
        f"| **Topology (1 / 2 / 3+ Services)** | {t['monolithic_1_service_pct']}% / {t['two_services_pct']}% / {t['distributed_3plus_services_pct']}% | 59.0% (1) / 22.8% (2) / 18.0% (3+) |",
        "",
        "### Actionable Findings",
    ]
    if report["findings"]:
        for f in report["findings"]:
            lines.append(f"- {f}")
    else:
        lines.append("- All trace integrity, privacy, and telemetry benchmark checks passed.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate LLM/Agent traces against 2026 benchmarks and integrity rules.")
    parser.add_argument("trace_file", type=Path, help="Path to JSON or JSONL trace file")
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    args = parser.parse_args()

    text = args.trace_file.read_text(encoding="utf-8").strip()
    if not text:
        raw_items: List[Dict[str, Any]] = []
    elif text.startswith("["):
        raw_items = json.loads(text)
    else:
        raw_items = [json.loads(line) for line in text.splitlines() if line.strip()]

    spans = [normalize_span(x, idx) for idx, x in enumerate(raw_items) if isinstance(x, dict)]
    report = analyze_spans(spans)
    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(format_markdown(report))


if __name__ == "__main__":
    main()
