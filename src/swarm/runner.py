"""
swarm.runner — Swarm executor.

Orchestrates agent self-registration and work item dispatch:
  1. Loads all agent YAML specifications from src/agents/.
  2. Registers each agent with the API via POST /agents (idempotent).
  3. Executes work items across agents concurrently (bounded thread pool).
  4. Checks /posts/exists to skip already published items (idempotency).
  5. Calls llm.complete_with_metadata with failure isolation.
  6. Posts updates to the message board via POST /posts with dedupe_key.

Failure isolation guarantee:
  If an individual agent's LLM call fails, that agent posts a visible
  'failed' update with source='mock-fallback'. One agent's failure NEVER
  crashes or halts the swarm.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx

from board import config
from board.feed import slugify
from board.llm import complete_with_metadata, LLMResponse
from board.models import PostKind
from swarm.loader import AgentSpec, WorkItem, load_all_agent_specs

logger = logging.getLogger(__name__)


def compute_dedupe_key(agent_slug: str, item: WorkItem, force_id: str | None = None) -> str:
    """Calculate dedupe key: work_item.key or first 32 hex chars of sha256(agent_slug|title|task)."""
    if item.key:
        base_key = item.key
    else:
        raw = f"{agent_slug}|{item.title}|{item.task}"
        base_key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    if force_id:
        return f"{base_key}-{force_id}"
    return base_key


class WorkItemResult(tuple):
    """Result tuple (source, failed, is_duplicate) with extra submit_error attribute."""
    source: str
    failed: bool
    is_duplicate: bool
    submit_error: bool

    def __new__(cls, source: str, failed: bool, is_duplicate: bool, submit_error: bool = False):
        instance = super().__new__(cls, (source, failed, is_duplicate))
        instance.source = source
        instance.failed = failed
        instance.is_duplicate = is_duplicate
        instance.submit_error = submit_error
        return instance


class SwarmResult:
    """Summary of swarm execution."""

    def __init__(self):
        self.agents_registered: int = 0
        self.posts_submitted: int = 0
        self.posts_skipped: int = 0
        self.posts_failed: int = 0
        self.sources_used: dict[str, int] = {}
        self.duration_seconds: float = 0.0

    def record_post(
        self,
        source: str,
        is_failed: bool,
        is_duplicate: bool = False,
        submit_error: bool = False,
    ):
        if is_duplicate:
            self.posts_skipped += 1
            return
        if submit_error:
            self.posts_failed += 1
            return
        self.posts_submitted += 1
        self.sources_used[source] = self.sources_used.get(source, 0) + 1


def register_agent_http(client: httpx.Client, base_url: str, spec: AgentSpec) -> str:
    """Register an agent via HTTP POST /agents. Returns slug."""
    payload = {
        "name": spec.name,
        "function": spec.function,
        "kind": spec.kind.value,
        "tags": spec.tags,
    }
    headers = {}
    if config.BOARD_API_KEY:
        headers["Authorization"] = f"Bearer {config.BOARD_API_KEY}"
    resp = client.post(f"{base_url}/agents", json=payload, headers=headers, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    return data["slug"]


def execute_work_item(
    client: httpx.Client,
    base_url: str,
    spec: AgentSpec,
    slug: str,
    item: WorkItem,
    force_id: str | None = None,
) -> tuple[str, bool, bool]:
    """Execute a single work item with idempotency check, LLM completion, and post to board.
    
    Returns: (source, is_failed, is_duplicate)
    """
    dedupe_key = compute_dedupe_key(slug, item, force_id=force_id)

    # Step 1: Pre-check if post exists BEFORE calling LLM (zero Claude/mock cost on duplicate)
    try:
        check_r = client.get(
            f"{base_url}/posts/exists",
            params={"agent_slug": slug, "dedupe_key": dedupe_key},
            timeout=5.0,
        )
        if check_r.status_code == 200 and check_r.json().get("exists") is True:
            return WorkItemResult("skipped", False, True, submit_error=False)
    except Exception as exc:
        logger.warning("Failed to pre-check /posts/exists for %s: %s", slug, exc)

    # Step 2: Format prompt and call LLM adapter with failure isolation
    fallback_text = f"Completed work item: {item.task} [verified]"
    llm_resp: LLMResponse
    try:
        prompt = spec.prompt_template.format(
            name=spec.name,
            function=spec.function,
            task=item.task,
            title=item.title,
        )
        llm_resp = complete_with_metadata(
            prompt,
            fallback_to_mock=True,
            fallback_text=fallback_text,
        )
    except Exception as exc:
        logger.warning("Execution failed for work item '%s' (%s): %s", item.title, slug, exc)
        llm_resp = LLMResponse(
            text=f"{fallback_text} [LLM unavailable: using fallback]",
            source="mock-fallback",
            failed=True,
            error_category="other",
        )

    # Determine post kind and source
    kind = PostKind.FAILED if (llm_resp.failed or item.kind == PostKind.FAILED) else item.kind
    combined_tags = list(set(spec.tags + item.tags))

    # Truncate post text to 5000 characters (ending with "…")
    post_text = llm_resp.text
    if len(post_text) > 5000:
        post_text = post_text[:4999] + "…"

    metadata = {
        **item.metadata,
        "work_item_title": item.title,
        "llm_source": llm_resp.source,
    }
    if llm_resp.failed:
        cat = getattr(llm_resp, "error_category", None) or "other"
        if cat not in {"timeout", "nonzero_exit", "empty_output", "missing_binary", "other"}:
            cat = "other"
        metadata["llm_error"] = cat

    # A3: Failed posts get a daily dedupe_key so at most ONE failed post per work item per day exists
    if llm_resp.failed:
        today_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        post_dedupe_key = f"failed-{dedupe_key}-{today_utc}"
    else:
        post_dedupe_key = dedupe_key

    post_payload = {
        "agent_slug": slug,
        "text": post_text,
        "kind": kind.value,
        "tags": combined_tags,
        "metadata": metadata,
        "source": llm_resp.source,
        "dedupe_key": post_dedupe_key,
    }

    headers = {}
    if config.BOARD_API_KEY:
        headers["Authorization"] = f"Bearer {config.BOARD_API_KEY}"

    try:
        r = client.post(f"{base_url}/posts", json=post_payload, headers=headers, timeout=10.0)
        r.raise_for_status()
        if r.status_code == 200 and r.json().get("status") == "duplicate":
            return WorkItemResult(llm_resp.source, False, True, submit_error=False)
    except Exception as exc:
        logger.error(f"Failed to submit post for {slug}: {exc}")
        return WorkItemResult(llm_resp.source, True, False, submit_error=True)

    return WorkItemResult(llm_resp.source, llm_resp.failed, False, submit_error=False)


def run_swarm(
    base_url: str,
    specs: list[AgentSpec] | None = None,
    max_workers: int | None = None,
    force: bool = False,
    client: httpx.Client | None = None,
) -> SwarmResult:
    """Execute the full agent swarm against a running API."""
    result = SwarmResult()
    start_time = time.monotonic()
    force_id = uuid.uuid4().hex[:8] if force else None

    agent_specs = specs if specs is not None else load_all_agent_specs()
    workers = max_workers or config.LLM_THREAD_POOL_SIZE

    print(f"[*] Starting Swarm Execution: {len(agent_specs)} agents configured (workers={workers}, force={force})")

    def _execute(http_client: httpx.Client) -> None:
        # Step 1: Self-register all agents
        slug_map: dict[str, str] = {}
        for spec in agent_specs:
            try:
                slug = register_agent_http(http_client, base_url, spec)
                slug_map[spec.name] = slug
                result.agents_registered += 1
            except Exception as exc:
                logger.error("Failed to register agent '%s': %s", spec.name, exc)
                # Fall back to deterministic slug
                slug_map[spec.name] = slugify(spec.name)

        print(f"[OK] Registered {result.agents_registered} agents with message board")

        # Step 2: Queue all work items
        tasks: list[tuple[AgentSpec, str, WorkItem]] = []
        for spec in agent_specs:
            slug = slug_map.get(spec.name, slugify(spec.name))
            for item in spec.work_items:
                tasks.append((spec, slug, item))

        print(f"[*] Executing {len(tasks)} work items across agent pool...")

        # Step 3: Run concurrently via bounded thread pool
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(execute_work_item, http_client, base_url, spec, slug, item, force_id)
                for (spec, slug, item) in tasks
            ]

            for future in as_completed(futures):
                try:
                    res = future.result()
                    source_used = res[0]
                    is_failed = res[1]
                    is_duplicate = res[2]
                    submit_err = getattr(res, "submit_error", False)
                    result.record_post(source_used, is_failed, is_duplicate=is_duplicate, submit_error=submit_err)
                except Exception as exc:
                    logger.error("Unexpected worker thread error: %s", exc)
                    result.record_post("mock-fallback", True, is_duplicate=False, submit_error=True)

    if client is not None:
        _execute(client)
    else:
        with httpx.Client() as c:
            _execute(c)

    result.duration_seconds = time.monotonic() - start_time
    print(f"[OK] Swarm completed in {result.duration_seconds:.2f}s:")
    print(f"     new: {result.posts_submitted}, skipped as duplicate: {result.posts_skipped}")
    print(f"     Sources breakdown: {result.sources_used}")
    return result


def run_swarm_cli(
    force: bool = False,
    loop: bool = False,
    interval: float = 5.0,
) -> int:
    """CLI handler for `python src/main.py swarm` with automated readiness polling and optional loop mode."""
    api_url = config.API_URL
    wait_seconds = getattr(config, "SWARM_WAIT_SECONDS", 60)
    start_wait = time.monotonic()
    ready = False

    print(f"[*] Connecting to API at {api_url} (polling /readyz up to {wait_seconds}s)...")
    while time.monotonic() - start_wait < wait_seconds:
        try:
            r = httpx.get(f"{api_url}/readyz", timeout=3.0)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(1.0)

    if not ready:
        print(f"[ERROR] Cannot connect to API server at {api_url} within {wait_seconds}s timeout.")
        print("\nThe 'swarm' command requires a running API server to post updates to.")
        print("Please choose one of the following:")
        print("  Option 1: Start the server in another terminal, then run swarm:")
        print("            python src/main.py serve")
        print("            python src/main.py swarm")
        print("\n  Option 2: Run the all-in-one demo (boots an in-process server automatically):")
        print("            python src/main.py demo\n")
        return 1

    print(f"[OK] API at {api_url} is ready. Proceeding with swarm execution.")
    if not loop:
        res = run_swarm(api_url, force=force)
        pf = getattr(res, "posts_failed", 0)
        failed_count = pf if isinstance(pf, (int, float)) else 0
        return 1 if failed_count > 0 else 0

    print(f"[*] Running swarm in continuous loop mode (interval={interval:.1f}s, press Ctrl+C to stop)...")
    cycle = 1
    last_cycle_failed = False
    try:
        while True:
            print(f"\n--- Swarm Cycle {cycle} ---")
            res = run_swarm(api_url, force=force)
            pf = getattr(res, "posts_failed", 0)
            failed_count = pf if isinstance(pf, (int, float)) else 0
            last_cycle_failed = (failed_count > 0)
            cycle += 1
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[!] Swarm continuous loop stopped by user.")
        return 1 if last_cycle_failed else 0
