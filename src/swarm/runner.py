"""
swarm.runner — Swarm executor.

Orchestrates agent self-registration and work item dispatch:
  1. Loads all agent YAML specifications from src/agents/.
  2. Registers each agent with the API via POST /agents (idempotent).
  3. Executes work items across agents concurrently (bounded thread pool).
  4. Calls llm.complete_with_metadata with failure isolation.
  5. Posts updates to the message board via POST /posts.

Failure isolation guarantee:
  If an individual agent's LLM call fails, that agent posts a visible
  'failed' update with source='mock-fallback'. One agent's failure NEVER
  crashes or halts the swarm.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx

from board import config
from board.feed import slugify
from board.llm import complete_with_metadata, LLMResponse
from board.models import PostKind
from swarm.loader import AgentSpec, WorkItem, load_all_agent_specs

logger = logging.getLogger(__name__)


class SwarmResult:
    """Summary of swarm execution."""

    def __init__(self):
        self.agents_registered: int = 0
        self.posts_submitted: int = 0
        self.posts_failed: int = 0
        self.sources_used: dict[str, int] = {}
        self.duration_seconds: float = 0.0

    def record_post(self, source: str, is_failed: bool):
        self.posts_submitted += 1
        if is_failed:
            self.posts_failed += 1
        self.sources_used[source] = self.sources_used.get(source, 0) + 1


def register_agent_http(client: httpx.Client, base_url: str, spec: AgentSpec) -> str:
    """Register an agent via HTTP POST /agents. Returns slug."""
    payload = {
        "name": spec.name,
        "function": spec.function,
        "kind": spec.kind.value,
        "tags": spec.tags,
    }
    resp = client.post(f"{base_url}/agents", json=payload, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    return data["slug"]


def execute_work_item(
    client: httpx.Client,
    base_url: str,
    spec: AgentSpec,
    slug: str,
    item: WorkItem,
) -> tuple[str, bool]:
    """Execute a single work item with LLM completion and post to board."""
    # Build prompt from template
    prompt = spec.prompt_template.format(
        name=spec.name,
        function=spec.function,
        task=item.task,
        title=item.title,
    )

    # Call LLM adapter with failure isolation
    llm_resp: LLMResponse
    try:
        llm_resp = complete_with_metadata(prompt, fallback_to_mock=True)
    except Exception as exc:
        # Extreme fallback in case adapter raises unexpectedly
        llm_resp = LLMResponse(
            text=f"Task execution failed: {item.task} ({exc})",
            source="mock-fallback",
            failed=True,
        )

    # Determine post kind and source
    kind = PostKind.FAILED if (llm_resp.failed or item.kind == PostKind.FAILED) else item.kind
    combined_tags = list(set(spec.tags + item.tags))

    post_payload = {
        "agent_slug": slug,
        "text": llm_resp.text,
        "kind": kind.value,
        "tags": combined_tags,
        "metadata": {
            **item.metadata,
            "work_item_title": item.title,
            "llm_source": llm_resp.source,
        },
        "source": llm_resp.source,
    }

    try:
        r = client.post(f"{base_url}/posts", json=post_payload, timeout=10.0)
        r.raise_for_status()
    except Exception as exc:
        logger.error(f"Failed to submit post for {slug}: {exc}")
        return llm_resp.source, True

    return llm_resp.source, llm_resp.failed


def run_swarm(
    base_url: str,
    specs: list[AgentSpec] | None = None,
    max_workers: int | None = None,
) -> SwarmResult:
    """Execute the full agent swarm against a running API."""
    result = SwarmResult()
    start_time = time.monotonic()

    agent_specs = specs if specs is not None else load_all_agent_specs()
    workers = max_workers or config.LLM_THREAD_POOL_SIZE

    print(f"[*] Starting Swarm Execution: {len(agent_specs)} agents configured (workers={workers})")

    with httpx.Client() as client:
        # Step 1: Self-register all agents
        slug_map: dict[str, str] = {}
        for spec in agent_specs:
            try:
                slug = register_agent_http(client, base_url, spec)
                slug_map[spec.name] = slug
                result.agents_registered += 1
            except Exception as exc:
                logger.error("Failed to register agent '%s': %exc", spec.name, exc)
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
                executor.submit(execute_work_item, client, base_url, spec, slug, item)
                for (spec, slug, item) in tasks
            ]

            for future in as_completed(futures):
                try:
                    source_used, is_failed = future.result()
                    result.record_post(source_used, is_failed)
                except Exception as exc:
                    logger.error("Unexpected worker thread error: %s", exc)
                    result.record_post("mock-fallback", True)

    result.duration_seconds = time.monotonic() - start_time
    print(f"[OK] Swarm completed in {result.duration_seconds:.2f}s:")
    print(f"     Posts submitted: {result.posts_submitted}")
    print(f"     Sources breakdown: {result.sources_used}")
    return result


def run_swarm_cli() -> None:
    """CLI handler for `python src/main.py swarm`."""
    run_swarm(config.API_URL)
