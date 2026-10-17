"""
test_p5_deploy.py — Verification suite for Phase P5 Deployment That Actually Deploys.

Covers:
  - Dockerfile hygiene: no gcc, dynamic port in healthcheck, non-root appuser.
  - Docker compose: Compose V2 specification without deprecated version, api and swarm services, no postgres.
  - Kubernetes manifests: single-replica deployment with Recreate strategy, swarm Job manifest, kustomize compilation.
  - Readiness polling in run_swarm_cli.
  - .gitignore rules for SQLite WAL/shm and data directory.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
import yaml
import pytest

from board.config import SRC_DIR
import board.config as config
from swarm.runner import run_swarm_cli

REPO_ROOT = SRC_DIR.parent
DEPLOY_DIR = REPO_ROOT / "deploy"
K8S_DIR = DEPLOY_DIR / "k8s"


def test_dockerfile_hygiene():
    """Ensure Dockerfile has no gcc bloat, creates appuser, and uses dynamic port in healthcheck."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "gcc" not in dockerfile
    assert "USER appuser" in dockerfile
    assert "os.getenv('PORT'" in dockerfile
    assert "FROM python:3.11-slim AS builder" in dockerfile
    assert "FROM python:3.11-slim AS runtime" in dockerfile


def test_docker_compose_v2_and_services():
    """Ensure docker-compose has no deprecated version header, has api and swarm, and no postgres."""
    compose_text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "version:" not in compose_text
    data = yaml.safe_load(compose_text)
    assert "api" in data["services"]
    assert "swarm" in data["services"]
    assert "postgres" not in data.get("services", {})


def test_k8s_job_and_single_replica_recreate_strategy():
    """Ensure deployment specifies replicas: 1, strategy: Recreate, and swarm Job exists."""
    dep = yaml.safe_load((K8S_DIR / "deployment.yaml").read_text(encoding="utf-8"))
    assert dep["spec"]["replicas"] == 1
    assert dep["spec"]["strategy"]["type"] == "Recreate"

    job = yaml.safe_load((K8S_DIR / "swarm-job.yaml").read_text(encoding="utf-8"))
    assert job["kind"] == "Job"
    assert "swarm" in job["spec"]["template"]["spec"]["containers"][0]["command"]

    kust = yaml.safe_load((K8S_DIR / "kustomization.yaml").read_text(encoding="utf-8"))
    assert "swarm-job.yaml" in kust["resources"]


def test_gitignore_contains_wal_and_data():
    """Ensure gitignore ignores sqlite wal/shm and data dir."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "*.db-wal" in gitignore
    assert "*.db-shm" in gitignore
    assert "data/" in gitignore


def test_swarm_cli_readiness_polling(monkeypatch, capsys):
    """Test that run_swarm_cli polls /readyz and executes swarm when ready."""
    calls = []

    def mock_get(url, timeout=None):
        calls.append(url)
        mock_resp = MagicMock()
        # Fail first 2 attempts, succeed on 3rd
        if len(calls) < 3:
            raise Exception("Connection refused")
        mock_resp.status_code = 200
        return mock_resp

    with patch("httpx.get", side_effect=mock_get):
        with patch("swarm.runner.run_swarm") as mock_run:
            monkeypatch.setattr(config, "SWARM_WAIT_SECONDS", 10)
            run_swarm_cli()
            mock_run.assert_called_once()
            captured = capsys.readouterr().out
            assert "is ready. Proceeding with swarm execution." in captured
