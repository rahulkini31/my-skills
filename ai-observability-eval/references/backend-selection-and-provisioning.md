# Backend Selection Matrix & Local Auto-Provisioning Guide

## Table of Contents
1. [Codebase Signals to Inspect](#1-codebase-signals-to-inspect)
2. [Local Backend Decision Matrix (Auto-Spawnable)](#2-local-backend-decision-matrix-auto-spawnable)
3. [Production Backend Decision Matrix (For PRD)](#3-production-backend-decision-matrix-for-prd)
4. [Automated Local Provisioning Templates (With OTel Collector)](#4-automated-local-provisioning-templates-with-otel-collector)
5. [Dual-Sink Instrumentation Wiring](#5-dual-sink-instrumentation-wiring)

---

## 1. Codebase Signals to Inspect

Before recommending a backend, inspect the workspace for these four signals:
1. **Container Runtime Availability:** Run `docker info >/dev/null 2>&1 && echo "DOCKER_OK" || echo "NO_DOCKER"` via shell tool.
2. **Orchestration & Language Stack:** Python (`langgraph`, `pydantic-ai`, `autogen`, `crewai`), TypeScript/Node (`ai` Vercel SDK, `mastra`, `@langchain/langgraph`), Java (`spring-ai`), or polyglot microservices.
3. **Model Diversity:** Count distinct providers (`google`, `anthropic`, `openai`, `bedrock`, `ollama`). If 2 or more providers are used, include a **Model Gateway** (LiteLLM Proxy, Envoy AI Gateway, or OpenRouter) in both local and production recommendations.
4. **Existing Cloud & APM Footprint:** Check Terraform, Helm charts, GitHub Actions, or `ddtrace` / `opentelemetry` packages for existing Datadog, AWS, GCP, or Kubernetes investments.

---

## 2. Local Backend Decision Matrix (Auto-Spawnable)

| Detected Environment | Recommended Local Stack | Why It Fits | Spawn Mechanism |
| :--- | :--- | :--- | :--- |
| **Python / TypeScript Multi-Agent + Docker Available (Default)** | **OTel Collector + Arize Phoenix + Jaeger** | Standards-compliant OTLP/HTTP (`:4318/v1/traces`) & gRPC (`:4317`) ingestion via OTel Collector, fanning out to Phoenix (`gen_ai.*` eval UI) and Jaeger (distributed service maps). | `docker compose -f docker-compose.observability.yml up -d` |
| **Heavy Prompt Versioning & Dataset Testing + Docker Available** | **OTel Collector + Langfuse Local Stack** | Best when the codebase relies on prompt registry management, human annotation queues, and dataset regression runs. | `docker compose -f docker-compose.langfuse.yml up -d` |
| **No Docker Daemon Running / Restricted Local Environment** | **Native CLI OTLP Receiver (`arize-phoenix` pip/uv)** | Runs in user space without container daemon requirements while exposing an OTLP HTTP collector (`http://localhost:6006/v1/traces`) and web UI. | `python3 -m phoenix.server.main serve` |

---

## 3. Production Backend Decision Matrix (For PRD)

| Codebase / Infra Signal | Recommended Production Observability + Gateway Stack | Key Production Capabilities to Specify in PRD |
| :--- | :--- | :--- |
| **Existing Datadog APM / Enterprise Multi-Cloud** | **Datadog LLM Observability + OpenTelemetry Collector Fleet + OpenRouter / Envoy AI Gateway** | Unified APM service maps with tool spans, out-of-the-box `429` rate-limit & prompt-cache dashboards, PII scrubbing at OTel Collector tier. |
| **Kubernetes / VPC Self-Hosted Requirement (Strict Data Residency)** | **Self-Hosted Langfuse or Arize Phoenix on K8s + OTel Collector DaemonSet + LiteLLM / Envoy AI Gateway** | Full VPC isolation, ClickHouse/S3 trace retention, centralized Redis-backed token-bucket rate limiting across agent replicas. |
| **Serverless / Cloud-Native (GCP Cloud Run or AWS ECS/Bedrock)** | **Managed OTLP Backend (Langfuse Cloud / Arize AX / GCP Cloud Trace) + Cloud-Native AI Gateway** | Zero-ops scaling, async pub/sub evaluation workers sampling 5%–10% of production traces for hallucination and trajectory scoring. |

---

## 4. Automated Local Provisioning Templates (With OTel Collector)

Once the author approves the local stack, write **both** `otel-collector-config.yaml` and `docker-compose.observability.yml` to the workspace and start the stack.

### Step 1: Write `otel-collector-config.yaml`
```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    timeout: 1s
    send_batch_size: 512

exporters:
  otlp/phoenix:
    endpoint: phoenix:4317
    tls:
      insecure: true
  otlp/jaeger:
    endpoint: jaeger:4317
    tls:
      insecure: true

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlp/phoenix, otlp/jaeger]
```

---

## 5. Dual-Sink Instrumentation Wiring

When wiring `observability/tracer.py` into the application after spawning the local backend:

1. Set default environment variables in `.env.observability` (or `.env`):

```bash
OTEL_SERVICE_NAME=multi-agent-app
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:6006/v1/traces
AGENT_TRACE_LOG_PATH=traces/agent_spans.jsonl
OTEL_GENAI_CAPTURE_CONTENT=false
```

2. Ensure the emitter writes both to disk and to the configured OTLP endpoint so the local evaluator can ingest JSONL while the collector forwards traces to Phoenix and Jaeger.

3. Keep the dual-sink flow non-blocking: append trace payloads to `AGENT_TRACE_LOG_PATH` and emit OTLP/HTTP POSTs asynchronously to avoid crashing the runtime if the collector is temporarily unavailable.
