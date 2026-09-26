# AI Engineering Remediation Playbooks

## Table of Contents
1. [Playbook 1: Cache-Friendly Prompt Layout Refactoring](#playbook-1-cache-friendly-prompt-layout-refactoring)
2. [Playbook 2: Enforcing Agent Loop Budgets & 429 Backpressure](#playbook-2-enforcing-agent-loop-budgets--429-backpressure)
3. [Playbook 3: Modular Model Gateway & Tiered Routing](#playbook-3-modular-model-gateway--tiered-routing)
4. [Playbook 4: Context Compression & Signal Hierarchy](#playbook-4-context-compression--signal-hierarchy)

---

## Playbook 1: Cache-Friendly Prompt Layout Refactoring

**Problem:** System instructions account for ~69% of input tokens, yet >70% of calls miss prompt cache reads because dynamic variables are injected early or JSON keys/tools are reordered across requests.

**Remediation Steps:**
1. **Strict Prefix Ordering:** Structure every prompt payload in strictly decreasing order of stability:
   - **Block 1 (Immutable across all requests):** Core system persona, safety/policy guardrails, and deterministically sorted tool schemas.
   - **Block 2 (Stable per session/project):** Static domain context, project rules, or cached reference documents.
   - **Block 3 (Append-only within session):** Multi-turn conversation history and prior tool execution turns.
   - **Block 4 (Dynamic per turn):** Current timestamp, volatile environment state, retrieved RAG snippets for the current query, and the active user message.
2. **Deterministic Serialization:** Sort tool definitions and JSON schema keys alphabetically (`sort_keys=True`) before constructing the prompt so serialization order never invalidates the prefix hash.

```text
[STATIC: System Instructions + Policy Guardrails + Sorted Tool Schemas]  <-- Cached Prefix
[SESSION-STABLE: Project Context / Persistent Memory]                   <-- Cached Prefix
[APPEND-ONLY: Prior Turn History]                                       <-- Incremental Cache
[DYNAMIC: Current Timestamp + Retrieved Docs + Current User Query]      <-- Uncached Tail
```

---

## Playbook 2: Enforcing Agent Loop Budgets & 429 Backpressure

**Problem:** Rate limits (`429`) cause 30%–60% of production LLM span failures. Variable-length ReAct loops and multi-agent retries amplify bursts into sustained outages.

**Remediation Steps:**

1. **Hard Execution Budgets:** Wrap every agent loop with explicit step, token, and wall-clock budgets that force graceful termination or fallback synthesis when exhausted:

```python
class AgentBudget:
    def __init__(self, max_calls: int = 8, max_input_tokens: int = 32000, max_cost_usd: float = 0.50):
        self.max_calls = max_calls
        self.max_input_tokens = max_input_tokens
        self.max_cost_usd = max_cost_usd
        self.calls_used = 0
        self.input_tokens_used = 0
        self.cost_used = 0.0

    def record_and_check(self, input_tokens: int, cost_usd: float = 0.0) -> None:
        self.calls_used += 1
        self.input_tokens_used += input_tokens
        self.cost_used += cost_usd
        if self.calls_used > self.max_calls:
            raise RuntimeError(f"BudgetExceeded: max_calls ({self.max_calls}) reached")
        if self.input_tokens_used > self.max_input_tokens:
            raise RuntimeError(f"BudgetExceeded: max_input_tokens ({self.max_input_tokens}) reached")
```

2. **Retry Discipline & Backpressure:**
   - Never retry `429` errors immediately inside inner tool loops.
   - Respect `Retry-After` headers; apply full-jitter exponential backoff (`sleep = random(0, min(cap, base * 2 ** attempt))`).
   - Implement an application-level token-bucket or concurrency semaphore when multiple agents share an organizational rate quota.

---

## Playbook 3: Modular Model Gateway & Tiered Routing

**Problem:** Over 70% of organizations run 3+ models (and 41% run 6+), creating tech debt, scattered credentials, and inconsistent failover.

**Remediation Steps:**

1. **Abstract Provider Calls:** Replace direct provider SDK calls in business logic with a unified gateway interface (or OpenRouter / Envoy AI Gateway / LiteLLM proxy) configured by task tier:
   - `tier: "fast-extraction"` -> Route to low-latency flash/haiku/mini models for classification, routing, and tagging.
   - `tier: "reasoning-synthesis"` -> Route to frontier models for complex planning, coding, and final synthesis.
2. **Automated Fallback & Sunset Registry:** Define a declarative model config with explicit fallback chains on `429`/`50x` errors and a `sunset_date` attribute on each model ID to flag tech debt in CI.

---

## Playbook 4: Context Compression & Signal Hierarchy

**Problem:** Median request size has doubled to 5,251 tokens (and quadrupled at P90), causing latency drift and burying critical details in noisy tool outputs.

**Remediation Steps:**

1. **Tool Output Gatekeeping:** Never dump raw API responses, full DOM trees, or unindexed logs directly into the agent transcript. Strip boilerplate fields, cap output length, and summarize or paginate responses exceeding 1,000 tokens.
2. **RAG Deduplication:** Deduplicate overlapping chunks by content hash or cosine similarity before prompt injection.
3. **Recency & Hierarchy Anchoring:** Place high-priority instructions and state summaries at the very beginning (system prefix) and very end (immediately preceding the final generation step) to mitigate lost-in-the-middle degradation.

---

This playbook set is intended as a practical reference for troubleshooting repeated LLM performance, reliability, and cost issues in production agent systems. Use it alongside the main evaluation workflow in the skill documentation for architectural diagnosis and remediation.
