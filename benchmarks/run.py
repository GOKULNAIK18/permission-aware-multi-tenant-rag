from __future__ import annotations

import argparse
import json
from importlib.metadata import PackageNotFoundError, version
import math
import os
import platform
import random
import statistics
import subprocess
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient, models
from sqlalchemy import delete, select

from app.auth.acl import authorize_chunk, build_acl_filter
from app.auth.dependencies import AuthenticatedUser
from app.auth.jwt import create_access_token
from app.core.config import get_settings
from app.db.database import SessionLocal
from app.main import app
from app.models.relational import AuditLog, Document, DocumentRole, DocumentUser, Role, Tenant, User, UserRole
from app.services.audit_service import AuditEventType, AuditLoggingService
from app.services.embedding_service import EmbeddingService
from app.services.permission_service import PermissionManagementService
from app.services.prompt_service import PromptBuilder
from app.services.qdrant_service import QdrantVectorStore
from app.services.rag_service import RAGService
from app.services.semantic_search_service import SemanticSearchResult, SemanticSearchService

ITERATIONS = int(os.getenv("BENCH_ITERATIONS", "30"))
WARMUPS = int(os.getenv("BENCH_WARMUPS", "5"))
DATASET_SIZES = [100, 1000, 5000]
BENCH_PREFIX = "milestone12_bench"
OUTPUT_JSON = Path(__file__).with_name("results_latest.json")
OUTPUT_MD = Path(__file__).with_name("results_latest.md")
QUERY_TEXT = "employee leave policy document retrieval"


class BenchLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.contexts: list[str] = []

    def complete(self, prompt: Any) -> str:
        self.calls += 1
        self.contexts.append(prompt.user_message)
        return "deterministic benchmark answer"


class StaticEmbedding:
    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def embed_text(self, _: str) -> list[float]:
        return [1.0] + [0.0] * (self.dimension - 1)


def _time_call(function: Callable[[], Any], count: int, warmups: int = WARMUPS) -> list[float]:
    for _ in range(warmups):
        function()
    timings = []
    for _ in range(count):
        started = time.perf_counter_ns()
        function()
        timings.append((time.perf_counter_ns() - started) / 1_000_000)
    return timings


def _percentile(samples: list[float], quantile: float) -> float:
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * quantile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def _stats(samples: list[float]) -> dict[str, float | int]:
    return {
        "iterations": len(samples),
        "min_ms": round(min(samples), 3),
        "mean_ms": round(statistics.fmean(samples), 3),
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(_percentile(samples, 0.95), 3),
        "p99_ms": round(_percentile(samples, 0.99), 3),
        "max_ms": round(max(samples), 3),
    }


def _cpu_name() -> str:
    try:
        proc = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return platform.processor() or "unavailable"


def _memory_gb() -> float | None:
    try:
        return round(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / (1024**3), 2)
    except (AttributeError, OSError, ValueError):
        return None


def _safe_service_location(url: str) -> dict[str, Any]:
    if url == ":memory:":
        return {"mode": "in_memory", "host": None, "port": None}
    parsed = urlsplit(url)
    return {"mode": "configured_service", "host": parsed.hostname, "port": parsed.port}


def _package_version(package_name: str) -> str:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return "unavailable"


def _qdrant_server_version() -> str:
    settings = get_settings()
    if settings.qdrant_url == ":memory:":
        return "not_applicable_in_memory"
    try:
        with httpx.Client(timeout=1.0) as client:
            response = client.get(settings.qdrant_url.rstrip("/"))
            response.raise_for_status()
            payload = response.json()
            return str(payload.get("version", "unreported"))
    except (httpx.HTTPError, ValueError):
        return "unavailable"


def _new_identity() -> tuple[AuthenticatedUser, str, AuthenticatedUser, str, AuthenticatedUser, str, int, int, int]:
    suffix = uuid.uuid4().hex[:10]
    with SessionLocal() as db:
        tenant = Tenant(name=f"bench-tenant-{suffix}")
        foreign_tenant = Tenant(name=f"bench-foreign-{suffix}")
        db.add_all([tenant, foreign_tenant])
        db.flush()
        roles: dict[str, Role] = {}
        for role_name in ("ADMIN", "EMPLOYEE", "HR"):
            role = db.scalar(select(Role).where(Role.name == role_name))
            if role is None:
                role = Role(name=role_name)
                db.add(role)
                db.flush()
            roles[role_name] = role
        user_rows: dict[str, User] = {}
        for label, owner, role_name in (
            ("employee", tenant, "EMPLOYEE"),
            ("admin", tenant, "ADMIN"),
            ("hr", tenant, "HR"),
            ("foreign", foreign_tenant, "EMPLOYEE"),
        ):
            user = User(
                tenant_id=owner.id,
                username=f"bench-{label}-{suffix}",
                password_hash="benchmark-only-not-a-credential",
                is_active=True,
            )
            db.add(user)
            db.flush()
            db.add(UserRole(user_id=user.id, role_id=roles[role_name].id))
            user_rows[label] = user
        db.commit()

        def principal(label: str, role_name: str) -> AuthenticatedUser:
            row = user_rows[label]
            return AuthenticatedUser(
                user_id=row.id,
                tenant_id=row.tenant_id,
                username=row.username,
                roles=[role_name],
            )

        employee = principal("employee", "EMPLOYEE")
        admin = principal("admin", "ADMIN")
        hr = principal("hr", "HR")
        foreign = principal("foreign", "EMPLOYEE")
        return (
            employee,
            create_access_token(employee.user_id, employee.tenant_id, employee.roles),
            hr,
            create_access_token(hr.user_id, hr.tenant_id, hr.roles),
            foreign,
            create_access_token(foreign.user_id, foreign.tenant_id, foreign.roles),
            tenant.id,
            foreign_tenant.id,
            admin.user_id,
        )


def _qdrant_available() -> bool:
    settings = get_settings()
    if settings.qdrant_url == ":memory:":
        return False
    try:
        with httpx.Client(timeout=1.0) as client:
            return client.get(f"{settings.qdrant_url.rstrip('/')}/collections").status_code == 200
    except httpx.HTTPError:
        return False


def _new_qdrant(mode: str, dimension: int, suffix: str) -> tuple[QdrantClient, QdrantVectorStore]:
    settings = get_settings()
    collection_name = f"{BENCH_PREFIX}_{mode}_{suffix}"[:60]
    client = QdrantClient(":memory:") if mode == "in_memory" else QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key or None,
        timeout=120,
        check_compatibility=False,
    )
    store = QdrantVectorStore(client, vector_size=dimension, collection_name=collection_name)
    store.ensure_collection()
    return client, store


def _payload(index: int, seed: int, tenant_id: int, foreign_tenant_id: int) -> dict[str, object]:
    is_target = index == 0
    return {
        "chunk_id": f"bench-{seed}-{index}",
        "document_id": seed * 1_000_000 + index + 1,
        "tenant_id": tenant_id if is_target or index % 2 == 0 else foreign_tenant_id,
        "text": f"Synthetic benchmark document {index}: employee leave policy.",
        "allowed_roles": ["EMPLOYEE"] if is_target or index % 3 == 0 else ["HR"],
        "allowed_users": [],
        "classification": "INTERNAL",
    }


def _load_synthetic_dataset(
    vector_store: QdrantVectorStore,
    count: int,
    tenant_id: int,
    foreign_tenant_id: int,
    seed: int,
) -> int:
    rng = random.Random(seed)
    payloads: list[dict[str, object]] = []
    vectors: list[list[float]] = []
    for index in range(count):
        payloads.append(_payload(index, seed, tenant_id, foreign_tenant_id))
        vectors.append([1.0] + [0.0] * (vector_store.vector_size - 1) if index == 0 else [rng.random() for _ in range(vector_store.vector_size)])
    for start in range(0, count, 500):
        vector_store.upsert_chunks(payloads[start : start + 500], vectors[start : start + 500])
    return seed * 1_000_000 + 1


def _qdrant_search_benchmark(
    mode: str,
    vector_store: QdrantVectorStore,
    embedding_vector: list[float],
    user: AuthenticatedUser,
    count: int,
    iterations: int,
    warmups: int,
) -> dict[str, Any]:
    tenant_only = models.Filter(
        must=[models.FieldCondition(key="tenant_id", match=models.MatchValue(value=user.tenant_id))]
    )
    acl_filter = build_acl_filter(user)
    results: dict[str, Any] = {}
    for label, query_filter in (
        ("unfiltered_baseline_component_only", None),
        ("tenant_acl", tenant_only),
        ("tenant_role_user_acl", acl_filter),
    ):
        timings = _time_call(
            lambda query_filter=query_filter: vector_store.search(embedding_vector, 10, query_filter),
            iterations,
            warmups,
        )
        retrieved = vector_store.search(embedding_vector, 10, query_filter)
        results[label] = {
            "mode": mode,
            "synthetic_chunks": count,
            "latency": _stats(timings),
            "result_count": len(retrieved),
            "baseline_only_not_secure_rag_path": query_filter is None,
        }
    return results


def _create_benchmark_document(tenant_id: int, roles: list[str]) -> int:
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        document = Document(
            tenant_id=tenant_id,
            name=f"benchmark-document-{suffix}",
            source_path="synthetic-only",
            classification="INTERNAL",
        )
        db.add(document)
        db.flush()
        for role_name in roles:
            role = db.scalar(select(Role).where(Role.name == role_name))
            if role is None:
                role = Role(name=role_name)
                db.add(role)
                db.flush()
            db.add(DocumentRole(document_id=document.id, role_id=role.id))
        db.commit()
        return document.id


def _insert_permission_chunk(store: QdrantVectorStore, document_id: int, tenant_id: int, roles: list[str]) -> str:
    chunk_id = f"bench-propagation-{document_id}"
    store.upsert_chunks(
        [{
            "chunk_id": chunk_id,
            "document_id": document_id,
            "tenant_id": tenant_id,
            "text": f"Synthetic permission benchmark content for {document_id}.",
            "allowed_roles": roles,
            "allowed_users": [],
            "classification": "INTERNAL",
        }],
        [[1.0] + [0.0] * (store.vector_size - 1)],
    )
    return chunk_id


def _permission_propagation(
    mode: str,
    client: QdrantClient,
    dimension: int,
    employee: AuthenticatedUser,
    employee_token: str,
    hr: AuthenticatedUser,
    hr_token: str,
    admin_id: int,
    admin_token: str,
) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    collection = f"{BENCH_PREFIX}_prop_{suffix}"[:60]
    store = QdrantVectorStore(client, dimension, collection)
    store.ensure_collection()
    revoke_doc = _create_benchmark_document(employee.tenant_id, ["EMPLOYEE"])
    revoke_chunk = _insert_permission_chunk(store, revoke_doc, employee.tenant_id, ["EMPLOYEE"])
    grant_doc = _create_benchmark_document(employee.tenant_id, [])
    grant_chunk = _insert_permission_chunk(store, grant_doc, employee.tenant_id, [])
    admin = AuthenticatedUser(user_id=admin_id, tenant_id=employee.tenant_id, username="benchmark-admin", roles=["ADMIN"])
    permission_service = PermissionManagementService(store)
    rag = RAGService(SemanticSearchService(StaticEmbedding(dimension), store), BenchLLM())  # type: ignore[arg-type]
    from app.api.permissions import get_permission_management_service
    from app.api.rag import get_rag_service

    app.dependency_overrides[get_permission_management_service] = lambda: permission_service
    app.dependency_overrides[get_rag_service] = lambda: rag
    try:
        with TestClient(app) as client_api:
            before_revoke = client_api.post("/rag/query", headers={"Authorization": f"Bearer {employee_token}"}, json={"question": QUERY_TEXT})
            if before_revoke.status_code != 200 or revoke_chunk not in {item["chunk_id"] for item in before_revoke.json()["retrieved_chunks"]}:
                raise RuntimeError("Revocation benchmark setup failed to retrieve the initially accessible chunk.")
            started = time.perf_counter_ns()
            response = client_api.patch(
                f"/documents/{revoke_doc}/permissions",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"allowed_roles": [], "allowed_users": []},
            )
            revoke_update_ms = (time.perf_counter_ns() - started) / 1_000_000
            if response.status_code != 200:
                raise RuntimeError("Revocation permission update failed.")
            rag.llm_client.calls = 0
            started = time.perf_counter_ns()
            after_revoke = client_api.post("/rag/query", headers={"Authorization": f"Bearer {employee_token}"}, json={"question": QUERY_TEXT})
            revoke_query_ms = (time.perf_counter_ns() - started) / 1_000_000
            if after_revoke.status_code != 200 or revoke_chunk in {item["chunk_id"] for item in after_revoke.json()["retrieved_chunks"]}:
                raise RuntimeError("Revocation did not take effect on the next secure query.")
            revoke_llm_calls = len(rag.llm_client.contexts)
            if revoke_llm_calls != 1:
                raise RuntimeError("Expected exactly the pre-revocation authorized call; empty post-revocation retrieval must skip LLM.")

            before_grant = client_api.post("/rag/query", headers={"Authorization": f"Bearer {hr_token}"}, json={"question": QUERY_TEXT})
            if before_grant.status_code != 200 or grant_chunk in {item["chunk_id"] for item in before_grant.json()["retrieved_chunks"]}:
                raise RuntimeError("Grant benchmark setup unexpectedly allowed retrieval before grant.")
            started = time.perf_counter_ns()
            response = client_api.patch(
                f"/documents/{grant_doc}/permissions",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"allowed_roles": ["HR"], "allowed_users": []},
            )
            grant_update_ms = (time.perf_counter_ns() - started) / 1_000_000
            if response.status_code != 200:
                raise RuntimeError("Grant permission update failed.")
            started = time.perf_counter_ns()
            after_grant = client_api.post("/rag/query", headers={"Authorization": f"Bearer {hr_token}"}, json={"question": QUERY_TEXT})
            grant_query_ms = (time.perf_counter_ns() - started) / 1_000_000
            if after_grant.status_code != 200 or grant_chunk not in {item["chunk_id"] for item in after_grant.json()["retrieved_chunks"]}:
                raise RuntimeError("Grant did not take effect on the next secure query.")
    finally:
        app.dependency_overrides.pop(get_permission_management_service, None)
        app.dependency_overrides.pop(get_rag_service, None)
        client.delete_collection(collection)
    return {
        "backend": mode,
        "permission_update_revoke": {"latency": _stats([revoke_update_ms])},
        "revocation_observation_next_query": {
            "latency": _stats([revoke_update_ms + revoke_query_ms]),
            "revoked_chunk_returned": False,
            "post_revoke_llm_calls": 0,
        },
        "permission_update_grant": {"latency": _stats([grant_update_ms])},
        "grant_observation_next_query": {
            "latency": _stats([grant_update_ms + grant_query_ms]),
            "new_chunk_returned": True,
        },
    }


def _secure_http_benchmark(
    mode: str,
    store: QdrantVectorStore,
    embedding: EmbeddingService,
    employee: AuthenticatedUser,
    employee_token: str,
    hr_token: str,
    foreign_token: str,
    target_doc_id: int,
    iterations: int,
    warmups: int,
) -> dict[str, Any]:
    target_chunk = f"bench-http-target-{mode}"
    store.upsert_chunks(
        [{
            "chunk_id": target_chunk,
            "document_id": target_doc_id,
            "tenant_id": employee.tenant_id,
            "text": "Synthetic secure HTTP benchmark context.",
            "allowed_roles": ["EMPLOYEE"],
            "allowed_users": [],
            "classification": "INTERNAL",
        }],
        [embedding.embed_text(QUERY_TEXT)],
    )
    llm = BenchLLM()
    rag = RAGService(SemanticSearchService(embedding, store), llm)  # type: ignore[arg-type]
    from app.api.rag import get_rag_service

    app.dependency_overrides[get_rag_service] = lambda: rag
    try:
        with TestClient(app) as client:
            responses: dict[str, list[httpx.Response]] = {"authorized": [], "unauthorized_role": [], "cross_tenant": []}
            times: dict[str, list[float]] = {key: [] for key in responses}
            call_counts: dict[str, int] = {}
            contexts: dict[str, list[str]] = {}
            tokens = {"authorized": employee_token, "unauthorized_role": hr_token, "cross_tenant": foreign_token}
            for case, token in tokens.items():
                for _ in range(warmups):
                    client.post(
                        "/rag/query",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"question": QUERY_TEXT, "limit": 5},
                    )
                llm.contexts.clear()
                before_calls = llm.calls
                for _ in range(iterations):
                    started = time.perf_counter_ns()
                    response = client.post("/rag/query", headers={"Authorization": f"Bearer {token}"}, json={"question": QUERY_TEXT, "limit": 5})
                    elapsed = (time.perf_counter_ns() - started) / 1_000_000
                    responses[case].append(response)
                    times[case].append(elapsed)
                call_counts[case] = llm.calls - before_calls
                contexts[case] = list(llm.contexts)
                llm.contexts.clear()
            target_retrieved = {
                case: any(
                    response.status_code == 200
                    and target_chunk in {item["chunk_id"] for item in response.json()["retrieved_chunks"]}
                    for response in responses[case]
                )
                for case in responses
            }
            target_in_context = {
                case: any(target_chunk in context for context in contexts[case])
                for case in responses
            }
            if not target_retrieved["authorized"] or not target_in_context["authorized"]:
                raise RuntimeError("Authorized JWT did not retrieve and send its synthetic target chunk to mock LLM.")
            if target_retrieved["unauthorized_role"] or target_retrieved["cross_tenant"]:
                raise RuntimeError("Unauthorized JWT retrieved the synthetic target chunk.")
            if target_in_context["unauthorized_role"] or target_in_context["cross_tenant"]:
                raise RuntimeError("Unauthorized synthetic target chunk reached the mock LLM.")

            concurrency: dict[str, Any] = {}
            for level in (1, 5, 10):
                sample_latencies: list[float] = []
                batch_times: list[float] = []
                errors = 0
                rounds = max(1, iterations // level)
                from concurrent.futures import ThreadPoolExecutor

                for _ in range(rounds):
                    batch_started = time.perf_counter_ns()

                    def one_request() -> tuple[int, float]:
                        started = time.perf_counter_ns()
                        response = client.post(
                            "/rag/query",
                            headers={"Authorization": f"Bearer {employee_token}"},
                            json={"question": QUERY_TEXT, "limit": 5},
                        )
                        return response.status_code, (time.perf_counter_ns() - started) / 1_000_000

                    with ThreadPoolExecutor(max_workers=level) as executor:
                        for status_code, latency in [future.result() for future in [executor.submit(one_request) for _ in range(level)]]:
                            sample_latencies.append(latency)
                            errors += status_code != 200
                    batch_times.append((time.perf_counter_ns() - batch_started) / 1_000_000)
                mean_batch_seconds = statistics.fmean(batch_times) / 1000
                concurrency[str(level)] = {
                    "latency": _stats(sample_latencies),
                    "throughput_requests_per_second": round(level / mean_batch_seconds, 3) if mean_batch_seconds else 0.0,
                    "iterations": len(sample_latencies),
                    "error_count": errors,
                    "kind": "FastAPI TestClient in-process; local characterization only",
                }

        result = {
            "backend": mode,
            "authorized": {
                "latency": _stats(times["authorized"]),
                "status_counts": _status_counts(responses["authorized"]),
                "mean_retrieved_chunks": _mean_results(responses["authorized"]),
                "target_retrieved": target_retrieved["authorized"],
                "target_reached_mock_llm": target_in_context["authorized"],
                "mock_llm_call_count": call_counts["authorized"],
            },
            "unauthorized_role": {
                "latency": _stats(times["unauthorized_role"]),
                "status_counts": _status_counts(responses["unauthorized_role"]),
                "mean_retrieved_chunks": _mean_results(responses["unauthorized_role"]),
                "target_retrieved": target_retrieved["unauthorized_role"],
                "target_reached_mock_llm": target_in_context["unauthorized_role"],
                "mock_llm_call_count": call_counts["unauthorized_role"],
            },
            "cross_tenant": {
                "latency": _stats(times["cross_tenant"]),
                "status_counts": _status_counts(responses["cross_tenant"]),
                "mean_retrieved_chunks": _mean_results(responses["cross_tenant"]),
                "target_retrieved": target_retrieved["cross_tenant"],
                "target_reached_mock_llm": target_in_context["cross_tenant"],
                "mock_llm_call_count": call_counts["cross_tenant"],
            },
            "concurrency": concurrency,
            "external_llm_calls": 0,
        }
        return result
    finally:
        app.dependency_overrides.pop(get_rag_service, None)


def _status_counts(responses: list[httpx.Response]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for response in responses:
        key = str(response.status_code)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _mean_results(responses: list[httpx.Response]) -> float:
    counts = [len(response.json().get("retrieved_chunks", [])) for response in responses if response.status_code == 200]
    return round(statistics.fmean(counts), 3) if counts else 0.0


def _component_benchmarks(
    mode: str,
    store: QdrantVectorStore,
    embedding: EmbeddingService,
    user: AuthenticatedUser,
    iterations: int,
    warmups: int,
) -> dict[str, Any]:
    vector = embedding.embed_text(QUERY_TEXT)
    acl_filter = build_acl_filter(user)
    chunk = SemanticSearchResult(
        payload={
            "chunk_id": "benchmark-component",
            "document_id": 1,
            "tenant_id": user.tenant_id,
            "text": "Synthetic component timing context.",
            "allowed_roles": user.roles,
            "allowed_users": [],
            "classification": "INTERNAL",
        },
        score=0.9,
    )
    builder = PromptBuilder()
    audit = AuditLoggingService()
    secure_service = RAGService(SemanticSearchService(embedding, store), BenchLLM(), audit_service=audit)
    no_audit_baseline = RAGService(
        SemanticSearchService(embedding, store),
        BenchLLM(),
        audit_service=NoAuditBaseline(),
    )
    secure_values = _time_call(lambda: secure_service.generate(QUERY_TEXT, user), iterations, warmups)
    no_audit_values = _time_call(lambda: no_audit_baseline.generate(QUERY_TEXT, user), iterations, warmups)
    audited_component_values = _time_call(
        lambda: audit.record_rag_event(user, AuditEventType.RAG_RETRIEVAL_SUCCEEDED, result_count=1),
        iterations,
        warmups,
    )
    return {
        "backend": mode,
        "acl_filter_construction": _stats(_time_call(lambda: build_acl_filter(user), iterations, warmups)),
        "warm_query_embedding": _stats(_time_call(lambda: embedding.embed_text(QUERY_TEXT), iterations, warmups)),
        "filtered_qdrant_search": _stats(_time_call(lambda: store.search(vector, 5, acl_filter), iterations, warmups)),
        "second_acl_check": _stats(_time_call(lambda: authorize_chunk(user, chunk), iterations, warmups)),
        "prompt_construction": _stats(_time_call(lambda: builder.build(QUERY_TEXT, [chunk]), iterations, warmups)),
        "required_audit_write": _stats(audited_component_values),
        "secure_rag_with_audit": _stats(secure_values),
        "no_audit_baseline_only": _stats(no_audit_values),
        "audit_median_delta_ms": round(statistics.median(secure_values) - statistics.median(no_audit_values), 3),
    }


class NoAuditBaseline:
    def record_rag_event(self, *args: Any, **kwargs: Any) -> None:
        return None


def _cleanup_database(tenant_ids: set[int]) -> None:
    if not tenant_ids:
        return
    with SessionLocal() as db:
        doc_ids = list(db.scalars(select(Document.id).where(Document.tenant_id.in_(tenant_ids))).all())
        user_ids = list(db.scalars(select(User.id).where(User.tenant_id.in_(tenant_ids))).all())
        if doc_ids:
            db.execute(delete(DocumentRole).where(DocumentRole.document_id.in_(doc_ids)))
            db.execute(delete(DocumentUser).where(DocumentUser.document_id.in_(doc_ids)))
            db.execute(delete(Document).where(Document.id.in_(doc_ids)))
        if user_ids:
            db.execute(delete(UserRole).where(UserRole.user_id.in_(user_ids)))
            db.execute(delete(AuditLog).where(AuditLog.user_id.in_(user_ids)))
            db.execute(delete(User).where(User.id.in_(user_ids)))
        db.execute(delete(AuditLog).where(AuditLog.tenant_id.in_(tenant_ids)))
        db.execute(delete(Tenant.__table__).where(Tenant.id.in_(tenant_ids)))
        db.commit()


def _cleanup_stale() -> None:
    with SessionLocal() as db:
        tenant_ids = set(
            db.scalars(select(Tenant.id).where(Tenant.name.like("bench-tenant-%") | Tenant.name.like("bench-foreign-%"))).all()
        )
    _cleanup_database(tenant_ids)
    settings = get_settings()
    if settings.qdrant_url == ":memory:":
        return
    try:
        client = QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
            timeout=5,
            check_compatibility=False,
        )
        names = [collection.name for collection in client.get_collections().collections if collection.name.startswith(f"{BENCH_PREFIX}_")]
        for name in names:
            client.delete_collection(name)
        client.close()
    except Exception:
        return


def _run(iterations: int, warmups: int, dataset_sizes: list[int]) -> dict[str, Any]:
    _cleanup_stale()
    settings = get_settings()
    started_at = datetime.now(timezone.utc).isoformat()
    employee, employee_token, hr, hr_token, foreign, foreign_token, tenant_id, foreign_tenant_id, admin_id = _new_identity()
    embedding = EmbeddingService()
    init_start = time.perf_counter_ns()
    dimension = embedding.dimension
    init_ms = (time.perf_counter_ns() - init_start) / 1_000_000
    sample_vector = embedding.embed_text(QUERY_TEXT)
    if len(sample_vector) != dimension:
        raise RuntimeError("Embedding dimension check failed.")

    available_modes = ["in_memory", "real_server"] if _qdrant_available() else ["in_memory"]
    report: dict[str, Any] = {
        "benchmark_timestamp_utc": started_at,
        "benchmark_kind": "local synthetic developer benchmark; not production performance",
        "environment": {
            "python_version": platform.python_version(),
            "qdrant_client_version": _package_version("qdrant-client"),
            "qdrant_server_version": _qdrant_server_version(),
            "operating_system": platform.platform(),
            "cpu": _cpu_name(),
            "logical_cpu_count": os.cpu_count(),
            "memory_total_gb": _memory_gb(),
            "qdrant_endpoint": _safe_service_location(settings.qdrant_url),
            "embedding_model": embedding.model_name,
            "embedding_dimension": dimension,
            "embedding_device": str(getattr(embedding._model(), "device", "unknown")),
            "database_driver": settings.database_url.split(":", 1)[0],
            "database_pool_size": settings.db_pool_size,
            "database_max_overflow": settings.db_max_overflow,
            "database_echo_enabled": settings.db_echo,
            "iterations_per_sample": iterations,
            "warmup_iterations": warmups,
            "synthetic": True,
            "external_llm_calls": 0,
        },
        "model_initialization": {"first_dimension_load_ms": round(init_ms, 3), "excluded_from_warm_embedding_latency": True},
        "acl_filter": {},
        "embedding": {},
        "qdrant_retrieval": {},
        "pipeline_components": {},
        "secure_http_pipeline": {},
        "permission_propagation": {},
        "limitations": [
            "All content and identities are synthetic and local; this is not a production capacity claim.",
            "Unfiltered Qdrant is an isolated baseline component only, never used by secure RAG.",
            "RAG HTTP samples use real JWT verification and audit persistence; LLM generation is deterministic and local.",
            "Permission propagation is one measured revoke/grant cycle per backend, not a distribution.",
            "Concurrency uses in-process FastAPI TestClient, not a networked load test of a deployed API server.",
        ],
    }

    filter_users = {
        "one_role": AuthenticatedUser(user_id=employee.user_id, tenant_id=employee.tenant_id, username=employee.username, roles=["EMPLOYEE"]),
        "multiple_roles": AuthenticatedUser(user_id=employee.user_id, tenant_id=employee.tenant_id, username=employee.username, roles=["EMPLOYEE", "HR"]),
        "user_specific": AuthenticatedUser(user_id=employee.user_id, tenant_id=employee.tenant_id, username=employee.username, roles=[]),
        "multiple_roles_and_user": AuthenticatedUser(user_id=employee.user_id, tenant_id=employee.tenant_id, username=employee.username, roles=["EMPLOYEE", "HR"]),
        "all_supported_roles": AuthenticatedUser(user_id=employee.user_id, tenant_id=employee.tenant_id, username=employee.username, roles=["ADMIN", "EMPLOYEE", "HR"]),
    }
    report["acl_filter"] = {
        key: _stats(_time_call(lambda case_user=case_user: build_acl_filter(case_user), iterations, warmups))
        for key, case_user in filter_users.items()
    }
    report["embedding"] = {
        "warm_query_embedding": _stats(_time_call(lambda: embedding.embed_text(QUERY_TEXT), iterations, warmups)),
        "device": str(getattr(embedding._model(), "device", "unknown")),
    }

    temp_collection_names: list[str] = []
    try:
        for mode_index, mode in enumerate(available_modes):
            client, initial_store = _new_qdrant(mode, dimension, uuid.uuid4().hex[:8])
            mode_data: dict[str, Any] = {}
            for size in dataset_sizes:
                if size == dataset_sizes[0]:
                    store = initial_store
                else:
                    store = QdrantVectorStore(client, dimension, f"{BENCH_PREFIX}_{mode}_{uuid.uuid4().hex[:8]}")
                    store.ensure_collection()
                    temp_collection_names.append(store.collection_name)
                temp_collection_names.append(store.collection_name)
                target_doc = _load_synthetic_dataset(
                    store,
                    size,
                    tenant_id,
                    foreign_tenant_id,
                    seed=(mode_index + 1) * 100_000 + size,
                )
                mode_data[str(size)] = _qdrant_search_benchmark(
                    mode,
                    store,
                    sample_vector,
                    employee,
                    size,
                    iterations,
                    warmups,
                )
                if size == dataset_sizes[-1]:
                    report["pipeline_components"][mode] = _component_benchmarks(mode, store, embedding, employee, iterations, warmups)
                    report["permission_propagation"][mode] = _permission_propagation(
                        mode,
                        client,
                        dimension,
                        employee,
                        employee_token,
                        hr,
                        hr_token,
                        admin_id,
                        create_access_token(admin_id, tenant_id, ["ADMIN"]),
                    )
                    report["secure_http_pipeline"][mode] = _secure_http_benchmark(
                        mode,
                        store,
                        embedding,
                        employee,
                        employee_token,
                        hr_token,
                        foreign_token,
                        target_doc,
                        iterations,
                        warmups,
                    )
            report["qdrant_retrieval"][mode] = {
                "backend": "local Qdrant server" if mode == "real_server" else "qdrant-client in-memory",
                "synthetic_chunk_counts": mode_data,
            }
            if mode == "in_memory":
                client.close()
    finally:
        settings = get_settings()
        if settings.qdrant_url != ":memory:":
            try:
                client = QdrantClient(
                    url=settings.qdrant_url,
                    api_key=settings.qdrant_api_key or None,
                    timeout=10,
                    check_compatibility=False,
                )
                for collection_name in set(temp_collection_names):
                    if collection_name.startswith(f"{BENCH_PREFIX}_"):
                        client.delete_collection(collection_name)
                client.close()
            except Exception:
                pass
        _cleanup_database({tenant_id, foreign_tenant_id})

    report["environment"]["tenants"] = 2
    report["environment"]["users"] = 4
    report["environment"]["roles"] = 3
    OUTPUT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    OUTPUT_MD.write_text(_render_markdown(report), encoding="utf-8")
    return report


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Milestone 12 Benchmark Results",
        "",
        f"Generated UTC: `{report['benchmark_timestamp_utc']}`",
        "",
        f"**Scope:** {report['benchmark_kind']}",
        "",
        "All values are measured in this run and rounded to 0.001 ms; they are not production performance claims.",
        "",
        "## Environment",
        "",
    ]
    for key, value in report["environment"].items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## ACL Filter Construction", "", "| Case | Min | Mean | Median | p95 | p99 | Max | n |", "|---|---:|---:|---:|---:|---:|---:|---:|"])
    for key, value in report["acl_filter"].items():
        lines.append(f"| {key} | {value['min_ms']:.3f} | {value['mean_ms']:.3f} | {value['median_ms']:.3f} | {value['p95_ms']:.3f} | {value['p99_ms']:.3f} | {value['max_ms']:.3f} | {value['iterations']} |")
    lines.extend(["", "## Warm Embedding", "", "| Measurement | Min | Mean | Median | p95 | p99 | Max | n |", "|---|---:|---:|---:|---:|---:|---:|---:|"])
    value = report["embedding"]["warm_query_embedding"]
    lines.append(f"| configured model, device {report['embedding']['device']} | {value['min_ms']:.3f} | {value['mean_ms']:.3f} | {value['median_ms']:.3f} | {value['p95_ms']:.3f} | {value['p99_ms']:.3f} | {value['max_ms']:.3f} | {value['iterations']} |")
    lines.extend(["", "## Qdrant Search", "", "Unfiltered baseline is component-only and never used by secure RAG.", ""])
    for mode, mode_data in report["qdrant_retrieval"].items():
        lines.extend([f"### {mode} ({mode_data['backend']})", "", "| Chunks | Search kind | Min | Mean | Median | p95 | p99 | Max | n | Results |", "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|"])
        for size, cases in mode_data["synthetic_chunk_counts"].items():
            for case, result in cases.items():
                latency = result["latency"]
                lines.append(f"| {size} | {case} | {latency['min_ms']:.3f} | {latency['mean_ms']:.3f} | {latency['median_ms']:.3f} | {latency['p95_ms']:.3f} | {latency['p99_ms']:.3f} | {latency['max_ms']:.3f} | {latency['iterations']} | {result['result_count']} |")
        lines.append("")
    lines.extend(["## Secure Pipeline Components", ""])
    for mode, components in report["pipeline_components"].items():
        lines.extend([f"### {mode}", "", "| Component | Min | Mean | Median | p95 | p99 | Max | n |", "|---|---:|---:|---:|---:|---:|---:|---:|"])
        for key, value in components.items():
            if not isinstance(value, dict) or "median_ms" not in value:
                continue
            lines.append(f"| {key} | {value['min_ms']:.3f} | {value['mean_ms']:.3f} | {value['median_ms']:.3f} | {value['p95_ms']:.3f} | {value['p99_ms']:.3f} | {value['max_ms']:.3f} | {value['iterations']} |")
        delta = components.get("audit_median_delta_ms")
        if isinstance(delta, (int, float)):
            lines.append(f"\nRequired-audit median delta versus isolated no-audit baseline: `{delta:.3f} ms`.")
        lines.append("")
    lines.extend(["## Secure HTTP RAG and Concurrency", ""])
    for mode, data in report["secure_http_pipeline"].items():
        lines.append(f"### {mode}")
        for case in ("authorized", "unauthorized_role", "cross_tenant"):
            item = data[case]
            latency = item["latency"]
            lines.append(f"- `{case}`: median `{latency['median_ms']:.3f} ms`, p95 `{latency['p95_ms']:.3f} ms`, p99 `{latency['p99_ms']:.3f} ms`, n={latency['iterations']}; target retrieved={item['target_retrieved']}, target in LLM={item['target_reached_mock_llm']}, status={item['status_counts']}.")
        lines.append("- Concurrency:")
        for level, item in data["concurrency"].items():
            latency = item["latency"]
            lines.append(f"  - {level} concurrent: median `{latency['median_ms']:.3f} ms`, p95 `{latency['p95_ms']:.3f} ms`, p99 `{latency['p99_ms']:.3f} ms`, throughput `{item['throughput_requests_per_second']:.3f} req/s`, errors={item['error_count']}.")
        lines.append("")
    lines.extend(["## Permission Propagation", ""])
    for mode, data in report["permission_propagation"].items():
        revoke = data["permission_update_revoke"]["latency"]
        revoke_observed = data["revocation_observation_next_query"]["latency"]
        grant = data["permission_update_grant"]["latency"]
        grant_observed = data["grant_observation_next_query"]["latency"]
        lines.append(
            f"- `{mode}` ({data['backend']}), n=1 each: revoke update median `{revoke['median_ms']:.3f} ms` "
            f"(min/mean/p95/p99/max are the same single sample); update+next-query deny `{revoke_observed['median_ms']:.3f} ms` "
            f"(revoked LLM calls={data['revocation_observation_next_query']['post_revoke_llm_calls']}); "
            f"grant update median `{grant['median_ms']:.3f} ms`; update+next-query grant `{grant_observed['median_ms']:.3f} ms`."
        )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {limitation}" for limitation in report["limitations"])
    lines.extend(["", "Full measured data: `results_latest.json`.", ""])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run reproducible local permission-aware RAG benchmarks.")
    parser.add_argument("--iterations", type=int, default=ITERATIONS)
    parser.add_argument("--warmups", type=int, default=WARMUPS)
    parser.add_argument("--sizes", default=",".join(map(str, DATASET_SIZES)))
    args = parser.parse_args()
    if args.iterations < 1 or args.warmups < 0:
        parser.error("iterations must be positive and warmups non-negative")
    sizes = [int(value.strip()) for value in args.sizes.split(",") if value.strip()]
    if not sizes or any(size < 1 for size in sizes):
        parser.error("sizes must contain positive integers")
    report = _run(args.iterations, args.warmups, sizes)
    print(f"Wrote {OUTPUT_JSON}")
    print(f"Wrote {OUTPUT_MD}")
    print(f"Measured Qdrant modes: {', '.join(report['qdrant_retrieval'])}")


if __name__ == "__main__":
    main()
