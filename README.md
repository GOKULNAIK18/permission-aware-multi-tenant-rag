# Permission-Aware Multi-Tenant RAG

A FastAPI demonstration of retrieval-augmented generation with tenant-scoped document access. PostgreSQL stores users, documents, ACLs, and audit metadata. Qdrant stores embedded chunks. Sentence Transformers supplies local embeddings, and answer generation uses a configurable OpenAI-compatible API.

The project exists to make authorization part of retrieval itself: a document the caller cannot access should not be selected for context or sent to the language model.

## Architecture

### RAG request flow

```text
User request
  → verify JWT and load active user
  → derive tenant and roles from verified identity and PostgreSQL
  → build tenant + (allowed role OR allowed user) filter
  → Qdrant applies the ACL filter before returning vector results
  → application validates every returned payload and ACL again
  → write required metadata-only audit event
  → build a prompt from authorized chunks
  → call the configured LLM
```

The Qdrant filter is the first authorization boundary. The application check is a second boundary for a faulty filter, unexpected payload, or compromised retriever. The second check is strict: malformed payloads, unknown roles or classifications, a missing field, or an unexpected extra field fail closed. If Qdrant returns any unauthorized chunk, the request is audited and aborted before prompt construction. An empty authorized result is audited and returns without calling the LLM.

Two checks are necessary because the vector store is an external system and should not be the only component trusted to enforce access. The essential rule is that unauthorized chunks never enter the prompt or reach the LLM; prompt instructions alone cannot guarantee that.

### Permission-management flow

```text
ADMIN JWT
  → verify active user and ADMIN role
  → find document by document ID and authenticated tenant ID
  → set matching Qdrant chunks to deny-all
  → update PostgreSQL ACL and commit
  → copy the new ACL to Qdrant
  → the next query evaluates the new permissions
```

PostgreSQL holds the relational document permission state. Qdrant payloads mirror the role, user, tenant, and classification fields needed during vector search. An ACL-only change does not re-embed text. The temporary deny-all state prevents a stale grant from being used while the two stores are synchronized. A synchronization failure is reported and the service attempts to leave the document denied; this can make a document temporarily unavailable until the permission update is retried.

## Security Model

- **Tenant isolation:** tenant ID is mandatory in every Qdrant chunk. Retrieval filters by the tenant from the authenticated identity before vector results are returned, then checks tenant again in application code.
- **JWT-derived identity:** the server verifies the token signature and expiry, loads the active user, and checks that token roles are consistent with database roles. The authenticated dependency supplies the identity used by protected routes.
- **No request-body identity:** RAG requests contain a question and retrieval limit. Upload tenant scope comes from the JWT. Permission changes ignore body identity fields and are scoped by the JWT tenant.
- **Role and user ACLs:** within a tenant, authorization succeeds when an allowed role matches or the authenticated user ID is explicitly allowed. Classification is validated and retained as metadata; classification alone does not grant access.
- **Qdrant pre-filter and second check:** the vector query uses tenant plus role/user conditions. The application requires the exact chunk payload shape and validates integer IDs, allowed role names, classification, and content type before accepting a result.
- **Fail closed:** malformed or unauthorized results cause an audited boundary failure and never reach prompt construction. A broken retriever cannot bypass the application check.
- **Prompt injection:** retrieved text is serialized as untrusted JSON context. System instructions tell the model to treat it as evidence, not instructions. Authorization stays outside the model and does not depend on model refusal.
- **Audit logging:** retrieval success, embedding/vector retrieval failure, and detected ACL-boundary failure are written with a unique request ID and authenticated identity. The query column is `[REDACTED]`; retrieval audit records contain event type, result count, and a bounded failure category, not document IDs, chunk IDs, model, prompts, responses, or retrieved text. Permission events record document ID and permission counts/classification, but no document or chunk content.
- **Required audit writes:** RAG writes its audit event before prompt construction or LLM generation. If the audit database write fails, the request returns an error and generation does not continue. Audit errors do not expose credentials or document text.
- **Permission changes:** only an authenticated ADMIN can modify permissions. Both the document and allowed users must belong to the authenticated tenant. Permission changes are audited and synchronized to Qdrant using deny-all during the transition.

Audit records are append-only through the application interfaces; there is no user-facing audit mutation endpoint.

## Threat Model

| Threat | Defense in this implementation |
|---|---|
| Cross-tenant retrieval | Tenant condition in the Qdrant pre-filter plus a second tenant check on every result. |
| Forged tenant, user, or role in a request | Protected routes derive identity from the verified JWT and database. Upload and permission routes do not use a caller-supplied tenant ID. |
| Malformed ACL metadata | Exact payload shape and strict type/value checks; invalid chunks fail closed before prompt creation. |
| Compromised or buggy retriever | Application-level ACL check rejects results outside the identity’s ACL and aborts before the LLM. |
| Prompt injection in retrieved documents | Retrieved content is untrusted context; access control is enforced before the model call. |
| Stale Qdrant ACL during revocation or grant | Permission update temporarily sets the document ACL to deny-all, then commits PostgreSQL state and applies the new Qdrant ACL. |
| Audit database failure | Required RAG audit write occurs before prompt construction; a failed write prevents generation and returns an error. |
| Unauthorized permission modification | ADMIN role is required and the document is selected using both document ID and authenticated tenant ID. |
| Sensitive data in logs or audit | Queries are redacted; retrieved text, prompts, responses, credentials, and tokens are excluded. Failure logs use IDs and bounded categories. |

## Data Model

### PostgreSQL

| Entity | Purpose |
|---|---|
| `tenants` | Organization identity and creation time. |
| `users` | Tenant membership, unique username, password hash, and active status. |
| `roles` | Role names such as ADMIN, HR, and EMPLOYEE. |
| `user_roles` | Many-to-many user/role assignments. |
| `documents` | Tenant-owned document name, source path, classification, and timestamps. |
| `document_roles` | Roles allowed to retrieve a document. |
| `document_users` | Users explicitly allowed to retrieve a document. |
| `audit_logs` | Request ID, actor and tenant IDs, redacted query field, outcome, timestamp, and metadata. |

Alembic owns the schema. PostgreSQL permission rows are the relational state; permission updates copy the resulting ACL metadata to every matching Qdrant chunk.

### Qdrant

The default collection is `chunks`. The runtime `qdrant-client` dependency and Docker server are both pinned to 1.19.1. Each point contains a vector and this payload:

| Field | Meaning |
|---|---|
| `chunk_id` | Stable application chunk identifier. |
| `document_id` | Parent PostgreSQL document ID. |
| `tenant_id` | Required tenant boundary. |
| `text` | Chunk text, used only after authorization. |
| `allowed_roles` | Roles allowed by the document ACL. |
| `allowed_users` | Explicitly allowed user IDs. |
| `classification` | PUBLIC, INTERNAL, or CONFIDENTIAL metadata. |

Qdrant has payload indexes for tenant, roles, users, and document ID. The insertion service rejects a missing tenant ID and validates the payload shape before upsert.

## API

All routes are served by FastAPI; `/docs` and `/openapi.json` expose the generated API description.

| Method and endpoint | Authentication | Purpose and request | Response and authorization |
|---|---|---|---|
| `GET /health` | None | Container/orchestrator health check. | `{"status":"ok"}`. |
| `POST /auth/login` | None | JSON `username`, `password`. | Bearer `access_token` and `token_type`; invalid credentials return 401. |
| `POST /documents/upload` | Bearer JWT with ADMIN role | Multipart `file`; optional `classification`, comma-separated `allowed_roles`, and comma-separated `allowed_users`. Tenant comes from the verified JWT; a submitted `tenant_id` is not used. | `document_id`, `chunk_count`; employee/non-admin users receive 403. Allowed users are checked against the authenticated tenant during ingestion. |
| `POST /rag/query` | Bearer JWT | JSON `question` and optional `limit` (1–20, default 5). | `answer` and `retrieved_chunks` with chunk ID, document ID, and score. ACL is enforced before and after retrieval. |
| `PATCH /documents/{document_id}/permissions` | Bearer JWT with ADMIN role | JSON `allowed_roles`, `allowed_users`, and optional `classification`. | Document ID, classification, and ACL counts. Cross-tenant documents are returned as not found; invalid ACL data is rejected. |
| `GET /protected` | Bearer JWT | Small authenticated identity diagnostic endpoint. | Returns the authenticated user ID, tenant ID, username, and database roles. |

No endpoint accepts request-body tenant or role values as authorization identity. Login and health are the only listed endpoints that do not require a Bearer token.

## Project Structure

```text
app/
├── api/                 # upload, RAG query, and permission routes
├── auth/                # JWT, authenticated identity, password hashing, ACL
├── core/                # settings
├── db/                  # SQLAlchemy base and database session
├── models/              # tenant, user, document, ACL, and audit models
├── schemas/              # request/response validation
└── services/             # ingestion, embeddings, Qdrant, RAG, audit, permissions, LLM
alembic/                  # migration environment and schema revisions
benchmarks/               # harness and saved Milestone 12 measurements
tests/                    # unit, API, permission, and adversarial security tests
.github/workflows/ci.yml  # GitHub Actions workflow
Dockerfile
docker-compose.yml
requirements-runtime.lock
requirements-dev.lock
README.md
```

Login is implemented in `app/main.py`; the API modules cover document upload, RAG, and permission changes.

## Setup and Development

### Prerequisites

- Python 3.13.1.
- Docker Desktop or Docker Engine with Docker Compose v2.
- Git when working from a repository clone.

### Environment configuration

```sh
cp .env.example .env
```

Replace every `REPLACE_WITH_LOCAL_ONLY_DATABASE_PASSWORD` occurrence, including `POSTGRES_PASSWORD` and both database URLs. Replace `JWT_SECRET` with a randomly generated secret of at least 32 bytes. Keep `.env` private: `.gitignore` excludes it, and `.dockerignore` keeps it out of the build context. The example contains placeholders, not usable credentials. With the safe `mock` provider selected, the Gemini key placeholder can remain unchanged.

`python scripts/seed_demo.py` creates fictional local demo accounts with fixed sample passwords. These credentials are public demo values, not production secrets; use the seeder only with an isolated local database and never deploy the seeded accounts. The script does not print password values.

### LLM provider

The default example uses `LLM_PROVIDER=mock`, a deterministic local client that makes no external request. To use Gemini through the existing OpenAI-compatible client, set these values in your private `.env`:

```dotenv
LLM_PROVIDER=openai-compatible
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_API_KEY=your_Gemini_API_key
LLM_MODEL=gemini-3.8-flash
LLM_TIMEOUT_SECONDS=60
```

Create the key through Google AI Studio and keep it only in environment configuration; never commit it or include it in logs or responses. Gemini's OpenAI-compatible endpoint and `gemini-3.8-flash` model are listed in Google's [OpenAI compatibility guide](https://ai.google.dev/gemini-api/docs/openai) and [model catalog](https://ai.google.dev/gemini-api/docs/models). Provider failures are returned to the application as a generic error without the key or remote error body. Tests use mocked transports and do not make a real Gemini API request.

### Native Python development

The native Python process connects to PostgreSQL and Qdrant through host URLs (`DATABASE_URL` and `QDRANT_URL`). Compose service-to-service connections use `postgres` and `qdrant`, never container-local `localhost`.

```sh
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install --requirement requirements-dev.lock
docker compose up -d postgres qdrant
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Check the service with `curl --fail http://127.0.0.1:8000/health`. The first real embedding operation may download the configured model and cache it locally.

### Tests

Tests use PostgreSQL, so start it and apply migrations before running them:

```sh
docker compose up -d postgres
alembic upgrade head
pytest -q
```

Run the adversarial/security group and the health route separately:

```sh
pytest -q tests/test_adversarial_security.py tests/test_permissions.py tests/test_leakage.py tests/test_acl.py tests/test_rag.py tests/test_audit.py
pytest -q tests/test_health.py
```

Tests use in-memory Qdrant and deterministic embedding/LLM doubles where appropriate. They do not require model downloads or a paid LLM. The in-memory client does not exercise Qdrant server payload-index behavior.

### Benchmarks

The benchmark command writes to the saved result paths, so run it only when intentionally refreshing Milestone 12 measurements:

```sh
.venv/bin/python -m benchmarks.run
```

Do not interpret local results as production capacity or SLA measurements. See [Benchmark Results](#benchmark-results) for the saved run documented here.

## Docker Deployment

Compose runs the API, PostgreSQL, and Qdrant. Published ports bind to loopback, database and vector data use named volumes, and the API image runs as an unprivileged user. Migrations are explicit so multiple app replicas do not race to migrate the schema.

After configuring `.env`:

```sh
docker compose up -d postgres qdrant
docker compose run --rm app alembic upgrade head
docker compose up --build -d app
docker compose ps
curl --fail http://127.0.0.1:8000/health
```

The container uses the internal database URL with host `postgres` and Qdrant URL with host `qdrant`. The first embedding may download model weights into the `model_cache` volume. To verify with isolated project resources and the documented alternate ports:

```sh
POSTGRES_PORT=15432 QDRANT_PORT=16333 APP_PORT=18000 docker compose -p rag-m14-check --env-file .env up -d postgres qdrant
POSTGRES_PORT=15432 QDRANT_PORT=16333 APP_PORT=18000 docker compose -p rag-m14-check --env-file .env run --rm app alembic upgrade head
POSTGRES_PORT=15432 QDRANT_PORT=16333 APP_PORT=18000 docker compose -p rag-m14-check --env-file .env up --build -d app
curl --fail http://127.0.0.1:18000/health
```

This uses separate named volumes. Remove only this isolated project's resources when finished; `down -v` deletes its volumes.

## Benchmark Results

These are the saved Milestone 12 measurements from 2026-09-29, not a new run. The machine was an Apple M1 with 8 GB RAM and Python 3.13.1 on macOS 27.0.1. Data and identities were synthetic (2 tenants, 4 users, 3 roles; vector datasets of 100, 1,000, and 5,000 chunks). Repeated measurements used n=30 after five warmups. The benchmark used Qdrant client 1.19.1 and local Docker Qdrant server 1.13.2; the deployment Compose file now uses server 1.19.1. Results therefore do not characterize the currently aligned deployment versions.

Filtered search (`tenant_role_user_acl`) and authenticated HTTP RAG figures below come from the saved report. Values are milliseconds; HTTP RAG used local FastAPI `TestClient` and a deterministic mocked LLM.

| Measurement | Backend / size | Median | p95 | n |
|---|---|---:|---:|---:|
| Tenant + role/user Qdrant search | In-memory, 100 chunks | 1.360 | 1.639 | 30 |
| Tenant + role/user Qdrant search | In-memory, 1,000 chunks | 9.919 | 10.638 | 30 |
| Tenant + role/user Qdrant search | In-memory, 5,000 chunks | 47.108 | 49.223 | 30 |
| Tenant + role/user Qdrant search | Docker Qdrant 1.13.2, 100 chunks | 2.360 | 3.026 | 30 |
| Tenant + role/user Qdrant search | Docker Qdrant 1.13.2, 1,000 chunks | 2.315 | 3.068 | 30 |
| Tenant + role/user Qdrant search | Docker Qdrant 1.13.2, 5,000 chunks | 2.501 | 3.282 | 30 |
| Authorized HTTP RAG with audit | Docker Qdrant 1.13.2, TestClient | 25.226 | 31.996 | 30 |

Permission propagation was measured once per backend (n=1), so these are individual observations rather than distributions:

| Backend | Revocation update | Next query denied | Grant update | Next query granted |
|---|---:|---:|---:|---:|
| In-memory Qdrant | 19.516 ms | 25.457 ms | 13.211 ms | 21.976 ms |
| Docker Qdrant 1.13.2 | 25.294 ms | 35.651 ms | 20.815 ms | 32.701 ms |

The report also measured ACL construction, warm embeddings, pipeline components, unauthorized HTTP cases, and in-process concurrency. It made no external LLM calls. TestClient is not a deployed network load test, the data is synthetic and local, and neither these values nor the n=1 propagation samples establish production performance or capacity. Full saved details are in [`benchmarks/results_latest.md`](benchmarks/results_latest.md) and [`benchmarks/results_latest.json`](benchmarks/results_latest.json).

## Verification and CI

The final local verification recorded for this workspace is:

- Full pytest suite: **124 passed**.
- Explicit security/adversarial selection, including upload tenant-spoof regression: **105 passed**.
- Health route test: **1 passed**.
- Docker application image build, Compose configuration, PostgreSQL/Qdrant health, Alembic migration, API health check: passed locally.

GitHub Actions is configured for pushes and pull requests. It installs `requirements-dev.lock`, starts PostgreSQL 16 and Qdrant 1.19.1 service containers, runs `alembic upgrade head`, executes the full suite, and explicitly executes security/leakage tests. CI credentials are test-only and `LLM_API_KEY` is empty; tests do not call an external LLM.

**GitHub Actions workflow is configured; remote GitHub-hosted execution must be verified after pushing to the repository.** The current workspace has no `.git` metadata, so tracked-file status and Git-based ignore checks are unavailable here. No commit or push was made.

## Known Limitations

- Benchmarks use synthetic local data on one Apple M1 developer machine with 8 GB RAM.
- Benchmark Qdrant server 1.13.2 differs from the current Compose server 1.19.1, despite the benchmark and deployment both using client 1.19.1.
- HTTP measurements use FastAPI `TestClient`, not a networked load test against a deployed API.
- Tests and saved benchmarks use mocked LLM generation; live Gemini generation requires a valid API key and network access.
- Permission propagation measurements are one revoke/grant cycle per backend (n=1).
- The GitHub Actions workflow has not been executed remotely from this workspace.
- Git metadata is unavailable in this workspace, so tracked-file and diff verification cannot be performed.
- Embedding model weights may need to be downloaded on first use.
