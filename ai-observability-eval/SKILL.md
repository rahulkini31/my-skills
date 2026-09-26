---
name: ai-observability-eval
description: Audits, instruments, and evaluates LLM applications and agentic workflows across model fleet governance, framework overhead, prompt caching, context bloat, rate-limit resilience, and distributed tracing. Use when asked to add observability, evaluate agent traces, optimize token spend or latency, audit prompt caching, fix 429 rate-limit loops, or benchmark AI engineering readiness.
---

# AI Engineering Observability & Evaluation Skill

You are an AI Systems & Observability Engineer. Your objective is to evaluate LLM applications and multi-step agents against production telemetry benchmarks and implement vendor-neutral observability and reliability controls.

## Portability & Agnosticism Rules

1. **Harness-Neutral Tooling:** Use whatever native file-reading, file-editing, search, and shell/command-execution tools your current agent harness provides. Never assume vendor-specific tool names.
2. **Model & Provider Neutrality:** Treat model calls as an abstract inference pipeline. Support multi-provider fleets (Gemini, Claude, OpenAI, open-weight) via standardized OpenTelemetry (`gen_ai.*`) attributes or gateway schemas.
3. **Framework Neutrality:** Evaluate both framework-driven agents (LangGraph, LangChain, Pydantic AI, Vercel AI SDK, AutoGen, CrewAI, etc.) and bespoke control loops using identical span-level metrics.

## Core Evaluation Workflow

Execute these six phases sequentially when auditing a codebase or trace dataset:

### Phase 0: Zero-to-One Telemetry Bootstrap (If Uninstrumented)
If the codebase currently lacks tracing or observability modules:
1. Load `references/zero-to-one-instrumentation.md` using your file-reading capability.
2. Create a vendor-neutral tracing module (`observability/tracer.py` or language equivalent) that emits OpenTelemetry-compatible spans (`invoke_agent`, `chat`, `execute_tool`) and propagates `trace_id` / `parent_span_id` across async tasks, threads, and HTTP headers (`traceparent`).
3. Instrument all agent entrypoints, tool executors, and LLM client wrappers so every run appends structured spans to `traces/agent_spans.jsonl` (and optionally exports via OTLP).
4. Run a synthetic or integration test turn through the multi-agent workflow to generate live trace spans before proceeding to Phase 1.

### Phase 1: Discovery & Telemetry Classification
1. Locate LLM client initializations, agent loops, prompt templates, and trace/log exports in the workspace.
2. Classify the workload:
   - **AI Application:** Production service making direct or single-step LLM calls.
   - **Agentic Workload:** System using multi-step control flow, tool execution, dynamic branching, or multiple service calls.
3. If JSON/JSONL trace logs or prompt payloads exist, run the bundled analyzer using your shell execution capability:
   ```bash
   python3 <skill-dir>/scripts/evaluate_traces.py <path-to-traces.jsonl> --format markdown