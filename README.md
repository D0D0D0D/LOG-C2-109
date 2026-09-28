# LOG-C2-109 — Customs Shipment Document Completeness Validator

> **Category**: Cat 2 (domain workflow)
> **Industry**: LOG

## Overview

This agent gives a trade-operations team a first pass over a shipment document packet **before** it
goes to a licensed customs broker. It checks the packet against an approved pre-review checklist,
compares the values that must agree across documents — product description, quantity, price,
country of origin, trade terms — and returns what is missing, what disagrees, what it could not
determine, which past return reasons are worth re-checking, and what to ask the broker.

What it will not do is the broker's job, and that boundary is enforced structurally rather than by
prompt instruction. HS classification, licence determinations, customs declarations and any
outbound transmission to a customs system are absent from the schema, and the output gate walks the
whole response — nested values included — and fails the run closed if a key or payload of that kind
appears, if a past HS/licence/declaration conclusion has been copied out of a recalled case, or if
a consistency finding arrives without a source identifier for the document it came from.

Two further properties shape the design:

- **An unknown is never rounded up to "fine".** OCR uncertainty, an ambiguous unit, an unmatched
  SKU, a missing explanatory document or an unclear rule all land in `unknowns` and go to the
  broker; nothing silently becomes "compliant" or "ready to declare".
- **An empty search and a broken search are different outcomes.** A knowledge-base miss returns an
  empty result with a stated degradation reason and `status: success`, routing the point to human
  review; a knowledge-base or backend failure is `status: error`. Conflating the two is how a
  system failure gets read as "nothing to report".

Because a customs broker must review the result, the graph pauses for that review: it interrupts,
carries the broker's approval, correction or rejection back in, and resumes on the same thread.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

| Endpoint | Purpose |
|---|---|
| `POST /invoke` | Validate one shipment document packet |
| `POST /resume` | Return the broker's decision for an interrupted session |
| `GET /health` | Liveness |

Provide:

- an AgentCore runtime with a checkpointer (the standalone server wires an in-memory one; a
  deployment that must survive a restart supplies a durable saver);
- a `BaseLLM` implementation injected through the graph configuration;
- authentication middleware that stamps a verified trust level and a `caller_id`, or the
  `INVOKE_AUTH_TOKEN` bearer for a standalone deployment;
- **the caller-to-customer mapping and the per-customer knowledge-base connections**. Both are
  empty by default (`caller_customer_map: {}`, `kb_connections: {}` in `config/config.yaml`), and
  the agent is fail-closed: an unmapped caller, an unknown customer, or a missing connection is a
  refusal, never a fallback to a default knowledge base;
- the two accumulated data layers: a database of SKU master data and item-to-required-document
  rules (each with owner, version, validity period and source), and a per-customer knowledge base
  of broker return reasons and queries that a human has already confirmed. Neither may contain HS,
  licence or declaration conclusions — the knowledge base supplies review *questions*, not answers.

Tenant separation is a property of the connection, not a filter on the rows: each customer's
knowledge base is physically separate, and which one is opened is decided from the authenticated
caller — never from the `customer_id` in the request body, which is only ever *compared* against
the authorised customer.

Runtime parameters live in `config/config.yaml`: `max_retry` (`3`), `timeout_seconds` (`30`),
`memory_enabled` (`true`), `hitl.enabled` (`true`) with `hitl.max_hitl` (`8`), `top_k` (`5`), plus
the `caller_customer_map` and `kb_connections` above.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent is designed to run on the platform; deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The standalone server in `src/api/server.py` starts without the platform — it wires the services,
compiles the graph with an in-memory checkpointer, and `/health` answers `{"status": "ok"}`. It
does not, however, invent an authorisation. A request with no verified identity and no matching
bearer token is **401**; a caller who is authenticated but not present in `caller_customer_map`
— which is the out-of-the-box state — gets **403** on both `/invoke` and `/resume`, because there is
no default customer and no fallback knowledge base to fall back to. Configuring the mapping and the
connections is what makes the agent reachable, and that is deliberate. The test suite runs without
a platform connection.

## Input

`POST /invoke` takes `input` (a JSON string), an optional `session_id` and an optional `thread_id`.
The envelope carries `customer_id`, `shipment_case_id`, `document_packet[]` and optional
`sku_refs[]`. The adapter puts the raw document bodies into a short-lived, customer- and
thread-scoped payload store on receipt and hands the graph only a reference plus a minimised
descriptor per document (`source_id`, a document type from a fixed set, an extraction confidence),
so raw document text never reaches graph state or a checkpoint. A malformed envelope is replaced by
an empty one rather than being passed through.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "input": "{\"customer_id\": \"cust-1\", \"shipment_case_id\": \"CASE-1\", \"document_packet\": [{\"source_id\": \"D1\", \"document_type\": \"invoice\"}]}",
    "session_id": "example-session"
  }'
```

`POST /resume` takes `thread_id` and `feedback`. Feedback is validated before it reaches the graph:
at most 4096 bytes, JSON-serialisable, nested no deeper than five levels, and an action of
`approve`, `correct` or `reject` (past-tense spellings accepted) — anything else is **422**. The
resuming caller must be authorised for the customer that owns the interrupted session; a mismatch
is **403** and an unknown thread is **404**, so an interrupted review cannot be answered by another
customer.

## Output

The standard AgentCore envelope is preserved. `output` carries five sections:

| Section | Content |
|---|---|
| `document_completeness` | Which required documents and explanatory items the approved checklist expects, and which are present |
| `consistency_findings` | Agreements and disagreements across documents, each with the source identifier it came from |
| `unknowns` | What could not be determined — OCR uncertainty, unknown units, unmatched SKUs, missing material, unclear rules |
| `similar_return_reasons` | Review points drawn from past broker returns and queries for the same customer |
| `broker_questions` | What to ask the customs broker, and what material is still missing |

`requires_human_review` marks a packet that needs a person. When the graph is invoked with human
review available it interrupts and waits; when it is invoked from an automated pipeline
(`hitl_allowed: false`) it does not deadlock — it skips the interrupt and leaves
`requires_human_review: true` in the output so the need for review is still recorded. All three
review outcomes (approved, corrected, rejected) are handled on resume.

## Security and Limitations

The professional boundary is the security core: HS classification, final licence decisions, customs
declarations and outbound transmission targets cannot appear in the output — the gate checks the
whole nested response against an allowlist of business keys rather than a denylist of forbidden
words, and a violation raises rather than being scrubbed. Knowledge-base recall is limited to review
questions; past HS, licence or declaration conclusions are not ingested and are rejected if they
appear in a recalled record.

Authorisation is fail-closed at every hop: the knowledge base is chosen from the authenticated
caller, a `customer_id` in the body is only compared against it, and an unmapped caller, an
unprovisioned connection, or a resume against another customer's session is refused. Raw document
bodies stay out of graph state behind a short-lived scoped reference.

Known limits, recorded rather than hidden:

- the standalone server uses an in-memory checkpointer, so interrupted sessions do not survive a
  restart; a deployment that needs that supplies a durable saver;
- retrieval returns a safe projection of knowledge-base records, so a record lacking an identifier
  is treated as a miss rather than being surfaced;
- document text extraction happens upstream; the agent consumes structured extraction results and
  cannot see an extraction error the packet does not report;
- output is assistive and does not guarantee acceptance of a declaration, a duty amount, an HS
  classification, a licence, or regulatory conformity. The declaration itself and every professional
  judgement remain with a qualified person.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), inner workflow subgraph
- Generation mode: `llm` (drafting and semantic recall; checklist matching and consistency comparison are deterministic)
- HITL: **enabled** — the graph interrupts for broker review and resumes on the same thread (`hitl.max_hitl: 8`)
- Persistence: checkpointer required; the standalone server wires an in-memory saver
- External API clients: deployment-injected; none bundled

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, a model, a database or network access. Running the agent
itself needs the platform, an injected `BaseLLM`, and the caller-to-customer mapping and
knowledge-base connections described above.

## Documentation

`docs/02_design.md` carries the node flow, the inner workflow stages, the state schema, the data
governance model, the professional-boundary output gate and the HITL design.
`docs/03_test_spec.md` records the test specification and its results.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
