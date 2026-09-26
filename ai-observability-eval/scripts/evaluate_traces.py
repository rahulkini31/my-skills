#!/usr/bin/env python3
"""
Model- and Harness-Agnostic AI Observability & Evaluation Trace Analyzer.
Computes the 7 core metrics from the 2026 State of AI Engineering benchmarks:
1. Provider & Model Fleet Diversity
2. Legacy Model Sprawl Detection
3. Framework Overhead & Call Fan-Out
4. Prompt Role Token Distribution (char_count / 4 proxy) & Cache Read Rate
5. Request Token Volume (P50 / P90)
6. Error Rate & 429 Rate-Limit Breakdown
7. Monolithic (1 service) vs. Distributed (3+ services) Agent Topology
"""

import argparse
import json
import math
import statistics
import sys
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


def estimate_tokens_from_chars(text: str) -> int:
    """Privacy-safe token approximation: divide character count by 4."""
    if not text:
        return 0
    return math.ceil(len(text) / 4.0)


def percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = (len(sorted_vals) - 1) * pct
    lower = math.floor(idx)
    upper = math.ceil(idx)
    if lower == upper:
        return float(sorted_vals[int(idx)])
    return float(sorted_vals[lower] * (upper - idx) + sorted_vals[upper] * (idx - lower))


def normalize_span(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalizes OpenTelemetry gen_ai.*, Datadog LLM, or generic JSON trace spans."""
    attrs = raw.get("attributes") or raw.get("meta") or {}
    usage = raw.get("usage") or attrs.get("usage") or {}

    trace_id = (
        raw.get("trace_id")
        or raw.get("traceId")
        or attrs.get("trace_id")
        or "unknown-trace"
    )
    service = (
        raw.get("service_name")
        or raw.get("service")
        or attrs.get("service.name")
        or "default-service"
    )
    provider = (
        raw.get("provider")
        or attrs.get("gen_ai.system")
        or attrs.get("llm.provider")
        or "unknown"
    ).lower()
    model = (
        raw.get("model")
        or attrs.get("gen_ai.request.model")
        or attrs.get("llm.model")
        or "unknown"
    ).lower()
    framework = (
        raw.get("framework")
        or attrs.get("llm.framework")
        or attrs.get("gen_ai.framework")
        or "none"
    )

    # Status / error normalization
    status_code = (
        raw.get("status_code")
        or attrs.get("http.status_code")
        or attrs.get("status_code")
        or 200
    )
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

    # Messages & Role Token Distribution (using char_count / 4 fallback)
    messages = (
        raw.get("messages")
        or attrs.get("gen_ai.prompt")
        or attrs.get("input.messages")
        or []
    )
    role_tokens: Dict[str, int] = defaultdict(int)
    if isinstance(messages, list):
        for msg in messages:
            if isinstance(msg, dict):
                role = str(msg.get("role", "other")).lower()
                if role not in ("system", "user", "developer", "assistant"):
                    role = "other"
                tok = msg.get("tokens")
                if tok is None:
                    content = msg.get("content", "")
                    if not isinstance(content, str):
                        content = json.dumps(content)
                    tok = estimate_tokens_from_chars(content)
                role_tokens[role] += int(tok)

    input_tokens = (
        usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or attrs.get("gen_ai.usage.input_tokens")
        or sum(role_tokens.values())
    )
    output_tokens = (
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or attrs.get("gen_ai.usage.output_tokens")
        or 0
    )
    cached_tokens = (
        usage.get("cached_read_input_tokens")
        or usage.get("cache_read_input_tokens")
        or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        or attrs.get("gen_ai.usage.cached_read_input_tokens")
        or 0
    )

    return {
        "trace_id": str(trace_id),
        "service": str(service),
        "provider": str(provider),
        "model": str(model),
        "framework": str(framework),
        "error_bucket": error_bucket,
        "role_tokens": dict(role_tokens),
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "cached_read_input_tokens": int(cached_tokens or 0),
    }


def load_spans(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        return [normalize_span(item) for item in data if isinstance(item, dict)]
    spans = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            spans.append(normalize_span(json.loads(line)))
    return spans


def analyze_spans(spans: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_spans = len(spans)
    if total_spans == 0:
        return {"error": "No spans found in input file."}

    providers = Counter(s["provider"] for s in spans if s["provider"] != "unknown")
    models = Counter(s["model"] for s in spans if s["model"] != "unknown")
    frameworks = Counter(s["framework"] for s in spans if s["framework"] != "none")

    legacy_models_found = sorted(
        {m for m in models if any(pat in m for pat in LEGACY_MODEL_PATTERNS)}
    )

    # Role token distribution
    total_role_tokens: Counter = Counter()
    for s in spans:
        for role, count in s["role_tokens"].items():
            total_role_tokens[role] += count
    sum_role_tokens = sum(total_role_tokens.values())
    role_shares = {
        role: round((cnt / sum_role_tokens) * 100, 1) if sum_role_tokens else 0.0
        for role, cnt in total_role_tokens.items()
    }

    # Prompt caching evaluation (only across models demonstrating cache support or all spans if flagged)
    models_with_cache = {
        s["model"] for s in spans if s["cached_read_input_tokens"] > 0
    }
    cache_eligible_spans = [
        s for s in spans if (s["model"] in models_with_cache) or not models_with_cache
    ]
    calls_with_cache_hit = sum(
        1 for s in cache_eligible_spans if s["cached_read_input_tokens"] > 0
    )
    cache_hit_call_rate = (
        round((calls_with_cache_hit / len(cache_eligible_spans)) * 100, 1)
        if cache_eligible_spans
        else 0.0
    )

    # Request/trace-level aggregation
    trace_tokens: Dict[str, int] = defaultdict(int)
    trace_services: Dict[str, set] = defaultdict(set)
    trace_spans_count: Counter = Counter()
    for s in spans:
        tid = s["trace_id"]
        trace_tokens[tid] += s["input_tokens"] + s["output_tokens"]
        trace_services[tid].add(s["service"])
        trace_spans_count[tid] += 1

    token_list = list(trace_tokens.values())
    p50_tokens = round(percentile(token_list, 0.50), 1)
    p90_tokens = round(percentile(token_list, 0.90), 1)

    # Error breakdown
    error_spans = [s for s in spans if s["error_bucket"] is not None]
    error_counts = Counter(s["error_bucket"] for s in error_spans)
    total_errors = len(error_spans)
    span_error_rate = round((total_errors / total_spans) * 100, 2)
    error_shares = {
        bucket: round((cnt / total_errors) * 100, 1) if total_errors else 0.0
        for bucket, cnt in error_counts.items()
    }

    # Service call topology per trace
    total_traces = len(trace_services)
    single_service_traces = sum(1 for svcs in trace_services.values() if len(svcs) == 1)
    two_service_traces = sum(1 for svcs in trace_services.values() if len(svcs) == 2)
    multi_service_traces = sum(1 for svcs in trace_services.values() if len(svcs) >= 3)

    # Generate actionable findings against 2026 benchmarks
    findings = []
    sys_share = role_shares.get("system", 0.0)
    if sys_share > 50.0 and cache_hit_call_rate < 28.0:
        findings.append(
            f"HIGH PRIORITY [Prompt Caching]: System prompts consume {sys_share}% of input tokens "
            f"(2026 avg: 69%), but only {cache_hit_call_rate}% of calls use cached reads (2026 baseline: 28%). "
            "Audit prompt layout for early dynamic variable injection."
        )
    if error_shares.get("429_rate_limit", 0.0) >= 25.0:
        findings.append(
            f"HIGH PRIORITY [Capacity Ceiling]: 429 rate-limit errors represent {error_shares['429_rate_limit']}% "
            "of all LLM failures. Enforce agent loop call/token budgets and jittered backoff."
        )
    if p50_tokens > 5251:
        findings.append(
            f"MEDIUM PRIORITY [Context Volume]: Median tokens per request ({p50_tokens}) exceeds the 2026 "
            "industry median (5,251). Audit tool outputs and RAG chunks for deduplication and compression."
        )
    if legacy_models_found:
        findings.append(
            f"LOW PRIORITY [Model Tech Debt]: Legacy model identifiers detected ({', '.join(legacy_models_found)}). "
            "Verify deprecation schedules and online evaluation coverage."
        )

    return {
        "summary": {
            "total_spans": total_spans,
            "total_traces": total_traces,
            "distinct_providers": len(providers),
            "distinct_models": len(models),
            "providers": dict(providers),
            "models": dict(models),
            "frameworks": dict(frameworks),
            "legacy_models_detected": legacy_models_found,
        },
        "context_and_caching": {
            "role_token_share_pct": role_shares,
            "cache_hit_call_rate_pct": cache_hit_call_rate,
            "p50_tokens_per_request": p50_tokens,
            "p90_tokens_per_request": p90_tokens,
            "max_spans_in_single_trace": max(trace_spans_count.values(), default=0),
        },
        "reliability": {
            "overall_span_error_rate_pct": span_error_rate,
            "error_breakdown_pct": error_shares,
        },
        "topology": {
            "monolithic_1_service_pct": round((single_service_traces / total_traces) * 100, 1) if total_traces else 0.0,
            "two_services_pct": round((two_service_traces / total_traces) * 100, 1) if total_traces else 0.0,
            "distributed_3plus_services_pct": round((multi_service_traces / total_traces) * 100, 1) if total_traces else 0.0,
        },
        "findings": findings,
    }


def format_markdown(report: Dict[str, Any]) -> str:
    if "error" in report:
        return f"**Error:** {report['error']}"
    s = report["summary"]
    c = report["context_and_caching"]
    r = report["reliability"]
    t = report["topology"]

    lines = [
        "## AI Observability & Evaluation Audit Report",
        "",
        "| Metric | Measured Value | 2026 Production Baseline |",
        "| :--- | :--- | :--- |",
        f"| **Distinct Models Used** | {s['distinct_models']} ({s['distinct_providers']} providers) | 70% of orgs use 3+ models; 41% use 6+ |",
        f"| **System Prompt Token Share** | {c['role_token_share_pct'].get('system', 0.0)}% | 69% system / 28% user / 2% developer |",
        f"| **Prompt Cache Read Rate** | {c['cache_hit_call_rate_pct']}% | 28% of calls on cache-capable models |",
        f"| **Median (P50) Tokens / Request** | {c['p50_tokens_per_request']} (P90: {c['p90_tokens_per_request']}) | 5,251 tokens median (doubled YoY) |",
        f"| **Overall Span Error Rate** | {r['overall_span_error_rate_pct']}% | 2% - 5% of all LLM spans |",
        f"| **429 Rate-Limit Error Share** | {r['error_breakdown_pct'].get('429_rate_limit', 0.0)}% | 30% - 60% of all LLM call errors |",
        f"| **Topology (1 / 2 / 3+ Services)** | {t['monolithic_1_service_pct']}% / {t['two_services_pct']}% / {t['distributed_3plus_services_pct']}% | 59.0% (1) / 22.8% (2) / 18.0% (3+) |",
        "",
        "### Actionable Findings",
    ]
    if report["findings"]:
        for f in report["findings"]:
            lines.append(f"- {f}")
    else:
        lines.append("- All core telemetry metrics are within healthy operational thresholds.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate LLM/Agent traces against 2026 benchmarks.")
    parser.add_argument("trace_file", type=Path, help="Path to JSON or JSONL trace file")
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    args = parser.parse_args()

    spans = load_spans(args.trace_file)
    report = analyze_spans(spans)
    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(format_markdown(report))


if __name__ == "__main__":
    main()