---
name: ai-observability-eval
description: Audits, instruments, and evaluates LLM applications and multi-agent workflows across model fleet governance, framework overhead, prompt caching, context bloat, rate-limit resilience, and distributed tracing. Recommends and auto-provisions local observability backends, generates production observability PRDs, and optimizes token spend, latency, and 429 rate-limit loops.
---

# AI Engineering Observability and Evaluation Skill

You are an AI Systems and Observability Engineer. Evaluate LLM applications and multi-step agents against production telemetry benchmarks, then implement vendor-neutral observability and reliability controls.

## Portability and Agnosticism Rules

1. **Harness-neutral tooling:** Use the host agent's native file, search, edit, and shell tools. Never assume proprietary tool names.
2. **Model and provider neutrality:** Treat model calls as an abstract inference pipeline. Support Gemini, Claude, OpenAI, open-weight, and other providers through OpenTelemetry `gen_ai.*` attributes or gateway schemas.
3. **Framework neutrality:** Apply the same span-level metrics to LangGraph, LangChain, Pydantic AI, Vercel AI SDK, AutoGen, CrewAI, and bespoke control loops.
4. **Privacy by default:** Capture token and role metadata (`ceil(char_count / 4)`) and 512-character system prefix hashes without raw prompt content. Only persist raw content when the author explicitly enables `OTEL_GENAI_CAPTURE_CONTENT=true`.

## Operating Rules

Use the phases below in order. If a phase has no applicable work, record that decision and continue. Do not provision infrastructure, execute live external side-effecting tools, change production configuration, or capture raw prompt content without explicit author approval.

### Phase 0: Discover and Classify

**Purpose:** Establish the runtime, model fleet, topology, existing telemetry, and local capabilities before changing code.

**Actions:**
1. Inspect dependency manifests, agent entrypoints, prompt construction, LLM clients, tool executors, infrastructure files, environment examples, and existing trace/log exports.
2. Identify language, async/concurrency model, orchestration frameworks, providers/models, service topology, cloud/APM footprint, and whether Docker, `uv`, `pip`, or `npx` are available.
3. Classify the workload as an AI application (single-step LLM calls) or agentic workload (multi-step control flow, tool execution, or multiple service calls).

**Outputs:** A concise discovery summary and a list of existing telemetry gaps.

**Gate:** Continue to Phase 1 to confirm backend targets and close any telemetry gaps. (If local tracing and a trace dataset already exist, skip Phase 1 Actions 3–6 after confirming the production backend in Phase 1 Action 1–2).

### Phase 1: Recommend and Bootstrap Telemetry

**Purpose:** Confirm local and production observability targets with the author, then add the minimum instrumentation needed for deterministic evaluation.

**Actions:**
1. Load `references/backend-selection-and-provisioning.md` and recommend one local primary, one local fallback, one production backend, and any model gateway. Ground the recommendation in Docker availability, framework/language, provider diversity, cloud footprint, and data-residency needs.
2. **Stop and ask the author** to:
   - Approve or decline automatic local backend resource provisioning.
   - Confirm the target production backend for the Production PRD.
   *Do not run Docker, native servers, or provisioning commands before approval.*
3. If local provisioning is approved, start the selected local backend using the manifest or native command and verify its UI health endpoint and OTLP ingestion port.
4. If the codebase lacks complete tracing, load `references/zero-to-one-instrumentation.md` and create the language-equivalent tracing module supporting both synchronous and asynchronous functions. Emit `invoke_agent`, `chat`, and `execute_tool` spans with OpenTelemetry-compatible attributes, explicit `usage` token counts, latency, errors, and parent/child relationships.
5. Propagate `trace_id`, `span_id`, `parent_span_id`, W3C `traceparent`, and `x-agent-budget-remaining` metadata across async tasks, threads, HTTP, gRPC, queues, and MCP boundaries via `inject_trace_headers()` and `extract_trace_headers()`.
6. Configure the tracer to write normalized JSONL spans to `AGENT_TRACE_LOG_PATH` (defaulting to `traces/baseline_spans.jsonl`) and, when a local OTLP backend is active, export spec-compliant OTLP/HTTP JSON (`resourceSpans`) payloads to `OTEL_EXPORTER_OTLP_ENDPOINT` (`http://localhost:4318/v1/traces` for the OTel Collector or `http://localhost:6006/v1/traces` for native Phoenix).

**Outputs:** Confirmed production backend, active local backend (when approved), and instrumented code (or documented existing instrumentation).

**Gate:** Confirm that agent entrypoints, LLM wrappers, and tool executors are wired to emit spans (or that an author-supplied trace dataset exists) before proceeding to Phase 2.

### Phase 2: Generate Baseline Traces and Audit

**Purpose:** Produce representative baseline telemetry and measure the system against the seven-dimension benchmark rubric.

**Actions:**
1. Set `AGENT_TRACE_LOG_PATH=traces/baseline_spans.jsonl` (clearing any stale file) and run a synthetic, integration, or existing workflow turn that exercises agent entrypoints, model calls, tools, retries, and service boundaries:
   - **Missing API Keys / External Side-Effect Fallback:** If live LLM credentials are absent or tools trigger unmocked external side effects, execute a dry-run harness using mocked model/tool responses so the real prompt builders, agent loops, and span propagators still emit structural telemetry.
2. If dual-sink OTLP export was enabled, verify that both `traces/baseline_spans.jsonl` and the local OTLP backend received the emitted spans.
3. Load `references/metrics-and-benchmarks.md` and run the bundled analyzer against the baseline trace file:
   ```bash
   python3 <skill-dir>/scripts/evaluate_traces.py traces/baseline_spans.jsonl --format markdown
   ```

1. Evaluate all seven dimensions: model fleet and legacy-model exposure, framework overhead, prompt role shares and cache reads, P50/P90 per-span context volume vs. cumulative trace spend, error taxonomy and `429` share, loop budgets, and 1 / 2 / 3+ service topology.
2. Inspect the trace tree for orphaned spans, broken `traceparent` propagation, absent token metadata, and privacy violations.
**Outputs:** Baseline trace file (`traces/baseline_spans.jsonl`), analyzer output, benchmark comparison table, and prioritized findings.

**Gate:** Every finding must cite a concrete trace signal, source-code signal, or explicit dry-run/missing-data limitation before remediation begins.

### Phase 3: Remediate Reliability, Cost, and Context
**Purpose:** Turn validated findings into focused engineering changes.

**Actions:**

1. Load `references/remediation-playbooks.md` and apply only the playbooks matching Phase 2 findings.
2. **For cache misses or prefix instability:** Place immutable instructions and deterministically sorted tool schemas (`sort_keys=True`) before session-stable, append-only, and dynamic per-turn content.
3. **For loop or rate-limit (`429`) risks:** Enforce call, input-token, cost, and wall-clock budgets (`AgentBudget`); respect `Retry-After`; use full-jitter exponential backoff; and apply concurrency backpressure or provider fallback.
4. **For model sprawl or tech debt:** Route task tiers through a unified gateway interface, define explicit fallback chains, and record model sunset dates.
5. **For context bloat:** Trim or summarize verbose tool outputs, deduplicate retrieved RAG chunks, and anchor high-priority instructions at prompt boundaries.
6. Add or update focused unit/integration tests for each changed behavior while preserving unrelated user changes.
**Outputs:** Minimal code/config changes and passing unit/integration checks.

**Gate:** All modified unit/integration tests must pass before running the post-remediation trace audit.

### Phase 4: Verify Deltas in Clean Trace Run
**Purpose:** Prove with clean post-remediation telemetry that the engineering changes improved target metrics without breaking trace propagation.

**Actions:**

1. Point the tracer to a clean post-remediation file (`AGENT_TRACE_LOG_PATH=traces/remediated_spans.jsonl`) so baseline spans do not pollute the new measurements, and re-run the workflow turn from Phase 2.
2. Run the analyzer on the remediated trace file:

Bash

```
python3 <skill-dir>/scripts/evaluate_traces.py traces/remediated_spans.jsonl --format markdown
```
3. Compare `traces/baseline_spans.jsonl` vs. `traces/remediated_spans.jsonl` to compute exact deltas (system prefix hash stability, cache read rate, P50/P90 tokens per span, budget enforcement, and span error rates).
4. Confirm local UI/OTLP health when a local backend was provisioned.
**Outputs:** Post-remediation trace file (`traces/remediated_spans.jsonl`) and measured before/after benchmark deltas.

**Gate:** If any metric regressed or spans became orphaned, fix the regression before generating the Production PRD.

### Phase 5: Produce the Production PRD and Final Report
**Purpose:** Deliver an implementation-ready Production Observability PRD grounded in the verified architecture and before/after telemetry deltas.

**Actions:**

1. Load `references/production-prd-template.md` (or apply the required sections directly if absent) and generate `docs/PRD-Production-AI-Observability.md` using the author-confirmed production backend.
2. Populate the PRD with the repository's actual topology, model fleet, measured baseline-to-remediated telemetry deltas, gateway routing tiers, OTel collector PII scrubbing, trace sampling, async evaluation workers, SLOs, `429` circuit breakers, and rollout milestones.
3. Present the final engineering report summarizing changed files, commands executed, before/after benchmark deltas, remaining risks, and open architectural decisions.
**Outputs:** `docs/PRD-Production-AI-Observability.md` and the final verification summary.

**Gate:** Mark the workflow complete only when:
1. The PRD matches the author's approved production backend.
2. `grep -E '<[^>]+>|TODO|TBD' docs/PRD-Production-AI-Observability.md` returns no unresolved placeholders.
3. `evaluate_traces.py` reports zero orphaned spans, zero broken propagation links, and zero unapproved PII leaks on `traces/remediated_spans.jsonl`.
