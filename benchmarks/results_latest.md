# Milestone 12 Benchmark Results

Generated UTC: `2026-09-29T15:57:06.870890+00:00`

**Scope:** local synthetic developer benchmark; not production performance

All values are measured in this run and rounded to 0.001 ms; they are not production performance claims.

## Environment

- `python_version`: 3.13.1
- `qdrant_client_version`: 1.19.1
- `qdrant_server_version`: 1.13.2
- `operating_system`: macOS-27.0.1-arm64-arm-64bit-Mach-O
- `cpu`: Apple M1
- `logical_cpu_count`: 8
- `memory_total_gb`: 8.0
- `qdrant_endpoint`: {'mode': 'configured_service', 'host': 'localhost', 'port': 6333}
- `embedding_model`: sentence-transformers/all-MiniLM-L6-v2
- `embedding_dimension`: 384
- `embedding_device`: mps:0
- `database_driver`: postgresql+psycopg
- `database_pool_size`: 5
- `database_max_overflow`: 10
- `database_echo_enabled`: False
- `iterations_per_sample`: 30
- `warmup_iterations`: 5
- `synthetic`: True
- `external_llm_calls`: 0
- `tenants`: 2
- `users`: 4
- `roles`: 3

## ACL Filter Construction

| Case | Min | Mean | Median | p95 | p99 | Max | n |
|---|---:|---:|---:|---:|---:|---:|---:|
| one_role | 0.008 | 0.008 | 0.008 | 0.008 | 0.009 | 0.009 | 30 |
| multiple_roles | 0.008 | 0.008 | 0.008 | 0.008 | 0.008 | 0.008 | 30 |
| user_specific | 0.005 | 0.005 | 0.005 | 0.005 | 0.005 | 0.005 | 30 |
| multiple_roles_and_user | 0.008 | 0.008 | 0.008 | 0.008 | 0.008 | 0.008 | 30 |
| all_supported_roles | 0.008 | 0.008 | 0.008 | 0.008 | 0.008 | 0.008 | 30 |

## Warm Embedding

| Measurement | Min | Mean | Median | p95 | p99 | Max | n |
|---|---:|---:|---:|---:|---:|---:|---:|
| configured model, device mps:0 | 9.957 | 15.269 | 13.270 | 25.910 | 28.552 | 28.562 | 30 |

## Qdrant Search

Unfiltered baseline is component-only and never used by secure RAG.

### in_memory (qdrant-client in-memory)

| Chunks | Search kind | Min | Mean | Median | p95 | p99 | Max | n | Results |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | unfiltered_baseline_component_only | 0.461 | 0.515 | 0.477 | 0.674 | 0.724 | 0.733 | 30 | 10 |
| 100 | tenant_acl | 0.895 | 9.115 | 1.009 | 1.786 | 172.162 | 241.730 | 30 | 10 |
| 100 | tenant_role_user_acl | 1.125 | 1.335 | 1.360 | 1.639 | 1.868 | 1.937 | 30 | 10 |
| 1000 | unfiltered_baseline_component_only | 0.647 | 0.743 | 0.670 | 1.190 | 1.567 | 1.643 | 30 | 10 |
| 1000 | tenant_acl | 5.216 | 5.842 | 5.752 | 6.323 | 6.380 | 6.402 | 30 | 10 |
| 1000 | tenant_role_user_acl | 9.061 | 9.896 | 9.919 | 10.638 | 11.012 | 11.123 | 30 | 10 |
| 5000 | unfiltered_baseline_component_only | 2.463 | 2.715 | 2.553 | 3.586 | 3.707 | 3.746 | 30 | 10 |
| 5000 | tenant_acl | 27.659 | 28.488 | 28.314 | 29.203 | 32.389 | 33.644 | 30 | 10 |
| 5000 | tenant_role_user_acl | 46.219 | 47.620 | 47.108 | 49.223 | 54.287 | 56.234 | 30 | 10 |

### real_server (local Qdrant server)

| Chunks | Search kind | Min | Mean | Median | p95 | p99 | Max | n | Results |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | unfiltered_baseline_component_only | 1.768 | 2.379 | 2.240 | 3.190 | 3.278 | 3.293 | 30 | 10 |
| 100 | tenant_acl | 1.884 | 2.392 | 2.251 | 3.266 | 3.433 | 3.479 | 30 | 10 |
| 100 | tenant_role_user_acl | 2.064 | 2.457 | 2.360 | 3.026 | 3.212 | 3.237 | 30 | 10 |
| 1000 | unfiltered_baseline_component_only | 2.004 | 2.522 | 2.426 | 3.095 | 3.154 | 3.164 | 30 | 10 |
| 1000 | tenant_acl | 2.153 | 2.491 | 2.394 | 3.030 | 3.061 | 3.064 | 30 | 10 |
| 1000 | tenant_role_user_acl | 1.960 | 2.410 | 2.315 | 3.068 | 3.220 | 3.254 | 30 | 10 |
| 5000 | unfiltered_baseline_component_only | 2.767 | 3.417 | 3.205 | 4.197 | 6.256 | 6.982 | 30 | 10 |
| 5000 | tenant_acl | 2.554 | 2.952 | 2.849 | 3.480 | 3.930 | 4.113 | 30 | 10 |
| 5000 | tenant_role_user_acl | 2.217 | 2.635 | 2.501 | 3.282 | 3.314 | 3.323 | 30 | 10 |

## Secure Pipeline Components

### in_memory

| Component | Min | Mean | Median | p95 | p99 | Max | n |
|---|---:|---:|---:|---:|---:|---:|---:|
| acl_filter_construction | 0.009 | 0.010 | 0.010 | 0.010 | 0.010 | 0.010 | 30 |
| warm_query_embedding | 8.189 | 10.984 | 10.621 | 13.739 | 15.646 | 16.194 | 30 |
| filtered_qdrant_search | 46.129 | 46.882 | 46.755 | 47.953 | 48.943 | 49.188 | 30 |
| second_acl_check | 0.001 | 0.001 | 0.001 | 0.001 | 0.002 | 0.002 | 30 |
| prompt_construction | 0.003 | 0.003 | 0.003 | 0.003 | 0.003 | 0.003 | 30 |
| required_audit_write | 1.196 | 1.557 | 1.353 | 2.740 | 2.921 | 2.951 | 30 |
| secure_rag_with_audit | 61.723 | 65.895 | 66.167 | 68.025 | 70.143 | 70.871 | 30 |
| no_audit_baseline_only | 57.368 | 58.600 | 58.451 | 60.270 | 61.122 | 61.257 | 30 |

Required-audit median delta versus isolated no-audit baseline: `7.715 ms`.

### real_server

| Component | Min | Mean | Median | p95 | p99 | Max | n |
|---|---:|---:|---:|---:|---:|---:|---:|
| acl_filter_construction | 0.013 | 0.013 | 0.013 | 0.014 | 0.014 | 0.014 | 30 |
| warm_query_embedding | 9.445 | 11.848 | 11.437 | 14.218 | 15.117 | 15.373 | 30 |
| filtered_qdrant_search | 3.217 | 7.302 | 4.666 | 17.801 | 39.960 | 47.387 | 30 |
| second_acl_check | 0.002 | 0.002 | 0.002 | 0.002 | 0.003 | 0.003 | 30 |
| prompt_construction | 0.004 | 0.004 | 0.004 | 0.005 | 0.005 | 0.005 | 30 |
| required_audit_write | 1.564 | 1.990 | 1.796 | 3.169 | 3.293 | 3.293 | 30 |
| secure_rag_with_audit | 16.333 | 18.342 | 17.959 | 21.555 | 22.710 | 22.893 | 30 |
| no_audit_baseline_only | 14.740 | 18.776 | 17.283 | 28.729 | 44.083 | 48.100 | 30 |

Required-audit median delta versus isolated no-audit baseline: `0.676 ms`.

## Secure HTTP RAG and Concurrency

### in_memory
- `authorized`: median `69.783 ms`, p95 `73.380 ms`, p99 `77.350 ms`, n=30; target retrieved=True, target in LLM=True, status={'200': 30}.
- `unauthorized_role`: median `69.596 ms`, p95 `78.901 ms`, p99 `88.928 ms`, n=30; target retrieved=False, target in LLM=False, status={'200': 30}.
- `cross_tenant`: median `72.676 ms`, p95 `122.593 ms`, p99 `213.730 ms`, n=30; target retrieved=False, target in LLM=False, status={'200': 30}.
- Concurrency:
  - 1 concurrent: median `73.065 ms`, p95 `76.850 ms`, p99 `76.997 ms`, throughput `13.710 req/s`, errors=0.
  - 5 concurrent: median `311.101 ms`, p95 `528.047 ms`, p99 `529.760 ms`, throughput `14.213 req/s`, errors=0.
  - 10 concurrent: median `609.896 ms`, p95 `649.572 ms`, p99 `653.618 ms`, throughput `15.768 req/s`, errors=0.

### real_server
- `authorized`: median `25.226 ms`, p95 `31.996 ms`, p99 `32.918 ms`, n=30; target retrieved=True, target in LLM=True, status={'200': 30}.
- `unauthorized_role`: median `23.976 ms`, p95 `27.951 ms`, p99 `36.930 ms`, n=30; target retrieved=False, target in LLM=False, status={'200': 30}.
- `cross_tenant`: median `24.770 ms`, p95 `26.305 ms`, p99 `26.908 ms`, n=30; target retrieved=False, target in LLM=False, status={'200': 30}.
- Concurrency:
  - 1 concurrent: median `22.919 ms`, p95 `25.528 ms`, p99 `25.941 ms`, throughput `42.855 req/s`, errors=0.
  - 5 concurrent: median `87.303 ms`, p95 `92.213 ms`, p99 `93.095 ms`, throughput `56.922 req/s`, errors=0.
  - 10 concurrent: median `140.044 ms`, p95 `157.507 ms`, p99 `159.389 ms`, throughput `67.628 req/s`, errors=0.

## Permission Propagation

- `in_memory` (in_memory): revoke update `19.516 ms`; next query observed deny in `25.457 ms` (LLM calls=0); grant update `13.211 ms`; next query observed grant in `21.976 ms`.
- `real_server` (real_server): revoke update `25.294 ms`; next query observed deny in `35.651 ms` (LLM calls=0); grant update `20.815 ms`; next query observed grant in `32.701 ms`.

## Limitations

- All content and identities are synthetic and local; this is not a production capacity claim.
- Unfiltered Qdrant is an isolated baseline component only, never used by secure RAG.
- RAG HTTP samples use real JWT verification and audit persistence; LLM generation is deterministic and local.
- Permission propagation is one measured revoke/grant cycle per backend, not a distribution.
- Concurrency uses in-process FastAPI TestClient, not a networked load test of a deployed API server.

Full measured data: `results_latest.json`.
