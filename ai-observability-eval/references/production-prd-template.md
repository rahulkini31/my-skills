# Production Observability PRD Generation Blueprint

When generating `docs/PRD-Production-AI-Observability.md`, populate every section below using concrete findings from the repository analysis and local trace baseline. Never leave generic placeholders.

---

# PRD: Production AI Observability, Model Gateway & Continuous Evaluation Platform

**Document Status:** Draft for Engineering & Platform Review
**Target System:** `<Insert Repository / Application Name>`
**Approved Production Stack:** `<Insert Author-Approved Production Backend & Gateway>`

## 1. Executive Summary & Problem Statement
* **Current Architecture:** Summarize the application's detected agent count, orchestration framework (`<Detected Framework>`), model fleet (`<Detected Models>`), and service boundaries (`<1 / 2 / 3+ Services>`).
* **Operational Risk:** Without production-grade distributed tracing, centralized model routing, and automated evaluations, changes to prompts, retrieval logic, or upstream model weights silently degrade latency, token spend, and failure rates.
* **Baseline Telemetry Findings:** Insert the initial local audit table produced by `scripts/evaluate_traces.py` (System Prompt Share %, Cache Hit Rate %, P50/P90 Span Tokens, Error Rate %, and Service Topology).

## 2. Production Architecture & Data Flow
Detail the four-tier target architecture tailored to the organization's cloud environment:
1. **Application & Agent Instrumentation Tier:**
   - OpenTelemetry `gen_ai.*` semantic conventions across all `invoke_agent`, `chat`, and `execute_tool` operations.
   - Mandatory W3C `traceparent` and `x-agent-budget-remaining` header propagation across HTTP, gRPC, MCP, and async queue boundaries (addressing the 2026 benchmark where 41% of agents span 2+ service calls).
2. **Centralized AI Gateway & Quota Tier:**
   - Route all provider calls through `<Approved Gateway: e.g., Envoy AI Gateway / LiteLLM Proxy / OpenRouter>` rather than scattered direct SDK calls (70%+ of production orgs run 3+ models; 41% run 6+).
   - Configure declarative model tiers (`fast-extraction` vs. `reasoning-synthesis`), automated cross-provider failover on `429`/`50x`, and a model deprecation registry to prevent legacy model tech debt.
3. **OTLP Collector & Privacy Scrubbing Tier:**
   - Deploy an OpenTelemetry Collector fleet with batching, retry queues, and PII redaction processors.
   - Compute privacy-safe token proxies (`ceil(char_count / 4)`) and system prefix hashes (`SHA-256` of first 512 chars) by default; gate raw prompt/completion capture behind explicit opt-in environment flags (`OTEL_GENAI_CAPTURE_CONTENT`).
4. **Observability Backend & Online Evaluation Tier:**
   - Ingest traces into `<Approved Production Backend>` with service maps that include downstream tools and sub-agents.
   - Run an asynchronous evaluation worker consuming 5%–10% of production traces to score tool-selection accuracy, trajectory efficiency, and output groundedness.

## 3. Non-Functional Requirements, SLIs & SLOs (Grounded in 2026 Benchmarks)

| Domain | Service Level Indicator (SLI) | 2026 Industry Baseline | Target Production SLO | Alerting / Circuit-Breaker Trigger |
| :--- | :--- | :--- | :--- | :--- |
| **Prompt Caching** | Proportion of cache-eligible LLM spans with `cached_read_input_tokens > 0` | 28% of calls (System role = 69% of input tokens) | **$\ge 70\%$** for scaffolded agents | Page if cache hit rate drops $< 40\%$ over 15m or `system_prefix_hash` churn exceeds 20% |
| **Context Efficiency** | Median (P50) and P90 input tokens per LLM span | P50 = 5,251 tokens (doubled YoY; P90 quadrupled) | **P50 $\le$ `<Target>` / P90 $\le$ `<Target>`** | Warn when tool or RAG payloads push P90 span tokens above threshold |
| **Capacity & Rate Limits** | Overall LLM span error rate & `429` share of errors | 2%–5% error rate; `429` causes 30%–60% of failures | **$< 1.0\%$ total error rate; $< 10\%$ `429` share** | Enforce hard `AgentBudget` loop termination (`max_calls`, `max_input_tokens`) + gateway failover |
| **Tracer Overhead** | P99 latency added by telemetry & OTLP export | N/A | **$\le 5\text{ ms}$** (async non-blocking export) | Circuit-break span exporter if collector queue memory exceeds 80% |

## 4. Capacity Planning, Rate-Limit Backpressure & Cost Controls
* **Runaway Loop Protection:** Specify exact `max_calls`, `max_input_tokens`, and `max_cost_usd` budgets per agent entrypoint discovered in the codebase.
* **Shared Quota Protection:** Define distributed token-bucket rate limiting at the AI Gateway layer and full-jitter exponential backoff so multi-agent retry bursts cannot cascade into organizational quota exhaustion.
* **Prompt Prefix Governance:** Enforce CI checks verifying that static system instructions, policies, and deterministically sorted tool schemas precede any volatile per-turn state.

## 5. Security, Compliance & PII Governance
* **Redaction Policy:** Strip API keys, bearer tokens, email addresses, and government/financial identifiers in the OTLP Collector before spans leave the VPC.
* **Retention & Access Control:** Define hot trace retention (e.g., 14–30 days in `<Approved Production Backend>`) and cold archive retention for evaluation datasets.

## 6. Phased Rollout Milestones
* **Milestone 1 (Completed Locally):** Zero-to-one OTLP + JSONL instrumentation wired into codebase; local `<Approved Local Backend>` verified.
* **Milestone 2 (Week 1–2):** Deploy OTLP Collector + `<Approved Production Backend>` in staging; enforce CI regression checks using `scripts/evaluate_traces.py`.
* **Milestone 3 (Week 3–4):** Migrate direct provider SDK calls behind the centralized AI Gateway with `429` fallback chains and `AgentBudget` enforcement.
* **Milestone 4 (Week 5+):** Enable asynchronous online trajectory evaluators and automated model-swap regression suites.