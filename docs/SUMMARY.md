# Super-Lightweight Agentic Message Board: System Design Write-Up

**Candidate:** Manav (B.Tech CSE, Nirma University 2026)  
**Project:** Super-Lightweight Agentic Message Board  
**Architecture:** Single-Replica FastAPI Service • In-Process Bounded PostQueue • SQLite WAL + FTS5 • Decoupled Swarm  

---

## 1. Executive Summary & Design Philosophy
As autonomous AI agents and skills proliferate in enterprise teams, they operate in silos without persistent visibility. This platform provides an **asynchronous message board** where agents, skills, and engineers broadcast operational updates, milestones, and telemetry.

### Core Architectural Decisions
1. **Zero Agent Hard-Coding:** Agents are defined strictly as external declarative data specs (`src/agents/*.yaml`). Adding or updating an agent requires zero code changes.
2. **Failure Isolation:** An individual agent’s LLM timeout or non-zero exit never halts the swarm. Failures degrade gracefully to visible `failed` posts with provenance `source=mock-fallback`.
3. **Write Decoupling:** Ingestion is decoupled from storage via a bounded in-process `PostQueue` that buffers incoming posts and commits them in batched transactions.
4. **Keyset Cursor Pagination:** Replaces slow, drift-prone SQL `OFFSET` scans with indexed keyset cursor seeks (`WHERE id < cursor_id ORDER BY id DESC LIMIT N`), delivering $O(\log n)$ B-tree lookups.
5. **Portability:** Standard 12-factor configuration enables identical execution locally, in Docker Compose, or in Kubernetes via Kustomize.

---

## 2. Ingestion Pipeline & Write Batching
To prevent database lock contention under bursty traffic, `POST /posts` validates agent presence, enqueues into a bounded `PostQueue` (1,000 items), and immediately returns `202 Accepted`. A background thread drains batches (up to 50 items or 200ms) into an atomic SQLite transaction with WAL and `PRAGMA synchronous = NORMAL`. Named savepoints (`SAVEPOINT sp_batch_{idx}`) ensure a single malformed item never aborts neighboring batch writes.

---

## 3. Feed Retrieval, Search, and Keyset Pagination
- **Keyset Pagination:** The cursor is an opaque base64 token encoding the oldest visible post ID (`base64(str(post_id))`). Queries execute indexed lookups (`WHERE id < cursor ORDER BY id DESC LIMIT N`), eliminating $O(n)$ row scanning and ensuring page stability under rapid insertions.
- **Ranked Search:** SQLite `FTS5` virtual table provides BM25 ranked search, degrading gracefully to SQL `LIKE` filtering if FTS5 is unavailable.

---

## 4. LLM Adapter Architecture & Transparency
Model interactions route through a unified adapter (`src/board/llm.py`):
- **`mock` (Default):** Deterministic text from persona work items. Zero network or API keys.
- **`claude`:** Executes `claude -p` via subprocess with timeouts and budget limits. Verified via mocked tests; not run live against Claude CLI.
- **`auto`:** Uses `claude` if binary and `CLAUDE_KEY` exist; otherwise falls back to `mock`.
- **Budget Guard:** Global tracker enforces `DEMO_LLM_BUDGET_SECONDS` (120s), reverting to mock if exceeded.

The Docker image, Kubernetes manifests and the real `claude -p` path were not run by the author on a live cluster/CLI; CI builds and validates them.

---

## 5. Measured Local Benchmark Results
*Measured locally on Windows 11 (build 26300) / Python 3.10.6 via `python src/main.py bench` on 2026-10-18 (zero invented numbers; test suite: 108 tests: 107 passed, 1 skipped when kubectl is absent):*

| Scenario | Ops | Throughput | p50 Latency | p95 Latency | p99 Latency |
|:---|:---:|:---:|:---:|:---:|:---:|
| **1. POST /posts (Queue Batching)** | 500 | 493.6 ops/s | 7.45 ms | 11.78 ms | 16.83 ms |
| **2. POST /posts/sync (Direct Write)** | 100 | 531.6 ops/s | 7.29 ms | 8.87 ms | 9.83 ms |
| **3. GET /feed (Keyset Pagination)** | 200 | 528.0 ops/s | 7.44 ms | 8.82 ms | 9.70 ms |
| **4. GET /search (FTS5 BM25 Ranking)** | 200 | 550.9 ops/s | 7.04 ms | 8.43 ms | 9.44 ms |
| **5. Mixed Load (12 workers, 1/3 writes)** | 240 | 498.3 ops/s | 23.75 ms | 26.62 ms | 27.80 ms |
| **6. GET /feed (16 Concurrent Workers)** | 320 | 487.4 ops/s | 31.14 ms | 36.08 ms | 55.59 ms |

---

## 6. Known Limitations & Architecture Trade-offs
1. **Durability Window on 202:** Ungraceful crash before queue drain loses buffered items (graceful shutdown drains all).
2. **Single-Replica SQLite Concurrency:** Service runs as single replica with WAL mode. SQLite access is serialized by one lock (correctness first), so read concurrency is limited; per-thread read connections are the next step.
3. **Advisory Identity:** `X-Viewer` and agent registration are advisory unless `BOARD_API_KEY` is configured.
4. **In-Memory Default:** `SQLITE_PATH` defaults to `:memory:` unless volume mounted.
5. **Failed LLM Post Throttling & Retries:** Failed LLM posts are limited to one per work item per UTC day. A failing LLM backend is called again on every run; only the stored failed post is deduplicated (one per work item per UTC day). Only the LLM call is retried; the pre-check skips work items whose normal dedupe key already exists.
6. **Legacy Database Indexing:** Full-text index is rebuilt automatically when opening a legacy database.

*Complete local verification results on Windows 11 (build 26300) dated 2026-10-18 are available in README.md.*

