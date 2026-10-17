"""
swarm.loader — Loads and validates agent YAML specs.

Adding an agent requires only adding a new .yaml file in src/agents/.
No code modification is ever required.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
import yaml
from pydantic import BaseModel, Field

from board.config import AGENTS_DIR
from board.models import AgentKind, PostKind


class WorkItem(BaseModel):
    title: str = Field(..., min_length=1)
    task: str = Field(..., min_length=1)
    kind: PostKind = PostKind.UPDATE
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentSpec(BaseModel):
    name: str = Field(..., min_length=1)
    function: str = Field(..., min_length=1)
    kind: AgentKind = AgentKind.SKILL
    tags: list[str] = Field(default_factory=list)
    prompt_template: str = Field(..., min_length=1)
    work_items: list[WorkItem] = Field(default_factory=list)


def load_agent_spec(file_path: Path) -> AgentSpec:
    """Load and validate a single agent YAML file."""
    content = file_path.read_text(encoding="utf-8")
    raw = yaml.safe_load(content)
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid YAML content in {file_path}")
    return AgentSpec(**raw)


def load_all_agent_specs(directory: Path | None = None) -> list[AgentSpec]:
    """Find and load all .yaml and .yml files from agents directory."""
    target_dir = directory or AGENTS_DIR
    if not target_dir.exists():
        return []

    specs: list[AgentSpec] = []
    # Sort for deterministic processing order
    yaml_files = sorted(list(target_dir.glob("*.yaml")) + list(target_dir.glob("*.yml")))

    for yf in yaml_files:
        try:
            spec = load_agent_spec(yf)
            specs.append(spec)
        except Exception as exc:
            raise RuntimeError(f"Error parsing agent spec {yf.name}: {exc}") from exc

    return specs
