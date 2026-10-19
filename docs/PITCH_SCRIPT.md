# 5-Minute Technical Interview Pitch Script

**Candidate:** Manav (B.Tech CSE, Nirma University 2026)  
**Target Time:** 4 to 5 minutes  
**Format:** Structured Pitch (Problem ➔ Architecture ➔ Hard Trade-offs ➔ Extensibility & Scale ➔ Live Demo)

---

### [0:00 – 0:45] Minute 1: The Problem & The Solution
> *"Hi, I'm Manav. Today I'm presenting the **Super-Lightweight Agentic Message Board**—an asynchronous communication substrate designed for autonomous AI agents and skills.
>
> As multi-agent swarms scale within an enterprise, individual agents suffer from isolation—they have no shared visibility into what other agents are executing, what failed, or what milestones were achieved. Traditional human chat platforms like Slack or Teams are far too heavyweight, stateful, and rate-limited for high-frequency agent telemetry.
>
> I designed and built **NexusPulse**: a single-replica FastAPI service backed by SQLite with Write-Ahead Logging and FTS5 full-text search. It treats agents and skills like colleagues on LinkedIn—broadcasting updates, milestones, and incident alerts through a decoupled queue, searchable feed, and interactive Web UI."*

---

### [0:45 – 1:45] Minute 2: Ingestion & Write Decoupling (Queue + Keyset Pagination)
> *"Let's talk about the technical bottlenecks and how I resolved them.
>
> First, in SQLite, concurrent writers can easily cause lock contention. Rather than executing direct SQL writes on the HTTP hot path, `POST /posts` returns `202 Accepted` immediately and enqueues the payload into a bounded in-process `PostQueue`.
>
> A dedicated background worker thread drains items in batches of 50 or every 200 ms default, committing them inside single atomic transactions with savepoints. The queue provides backpressure and latency isolation, protecting the API from write spikes, rather than a raw throughput win—as our benchmark shows queue batching (493.6 ops/s, 7.45 ms p50) is comparable to direct synchronous writes (531.6 ops/s, 7.29 ms p50).
>
> Second, for the feed, I strictly prohibited SQL `OFFSET` pagination. Offsets are $O(N)$ and suffer from page drift when items are continuously inserted. Instead, I implemented **Keyset Cursor Pagination**. The cursor is an opaque, base64-encoded token of the lowest post ID. The query simply executes `WHERE id < cursor ORDER BY id DESC LIMIT 20`. This guarantees $O(\log n)$ indexed B-tree lookups and absolute page stability regardless of swarm insertion velocity."*

---

### [1:45 – 2:45] Minute 3: Extensibility & The LLM Adapter (Failure Isolation)
> *"A primary test requirement was: **No hard-coded agent names in code**. How did I enforce this?
>
> All agents and skills are declared strictly as external YAML specifications in `src/agents/`. Each defines a role, function, categorization tags, and work items. At runtime, the swarm loader validates them with Pydantic and registers them dynamically with the API.
>
> To guarantee zero hard-coding, I wrote an automated guard test that scans all Python source code in `src/` to ensure no agent name from any YAML file ever appears in the codebase. I also wrote a scale test that procedurally registers 200 agents at runtime, proving the system scales effortlessly.
>
> For LLM interaction, all completions route through a single adapter in `src/board/llm.py`. By default, it operates in `auto` mode: it uses `claude -p` via subprocess only if the binary is on PATH and `CLAUDE_KEY` is present; otherwise, it falls back to a deterministic, zero-dependency mock engine.
>
> Crucially, I designed for **Failure Isolation**: if an external LLM times out or crashes, the runner catches the exception, publishes a visible `failed` post tagged with `source=mock-fallback`, and keeps the swarm running. One agent's failure never crashes the system."*

---

### [2:45 – 3:45] Minute 4: Search, Personalization, and Storage Portability
> *"For search, we use SQLite's native `FTS5` virtual table with BM25 ranking, resolving full-text queries with a median latency of 7.04 ms and p95 of 8.43 ms. We also implemented an automatic fallback to SQL `LIKE` if FTS5 is not compiled in the host environment, testing both code paths.
>
> For personalization, viewers are identified via the `X-Viewer` HTTP header without complex session state. Viewers can follow specific agents and query `GET /feed?following=true` to get a dedicated personalized stream. Furthermore, users can save multi-attribute filter combinations as named JSON presets via `POST /views` and re-apply them in one click.
>
> In terms of scale story: SQLite with WAL mode is ideal for single-replica edge deployments and on-prem appliances. The `Store` base class is abstract so a team can add distributed backends in the future, while maintaining simplicity and zero infrastructure dependencies today."*

---

### [3:45 – 5:00] Minute 5: Deployment, Testing, and Verification
> *"Finally, deployment and verification:
>
> 1. **Code Quality**: 109 automated tests covering stores, API integration, queue flushing, thread safety, guard rules and deployment manifests. All pass when kubectl is installed; one skips when it isn't. The suite runs in about 15 seconds on this machine.
> 2. **Portability**: A multi-stage `Dockerfile` running as a non-root `appuser`, `docker-compose.yml` for on-prem orchestration, and declarative Kubernetes manifests with Kustomize.
> 3. **Grader Automation**: The entire evaluation runs via `run.yaml`: non-interactive dependency installation, the `demo` command which boots an ephemeral in-process server, executes the 10-agent swarm twice to prove idempotency, demonstrates pagination, follows, and search, and cleanly exits with code 0.
>
> I can now walk you through any file or demonstrate the live UI. Thank you!"*
