"""
test_deploy.py — Validates deployment configurations (Dockerfile, Compose, K8s).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
import yaml
import pytest

from board.config import SRC_DIR

REPO_ROOT = SRC_DIR.parent
DEPLOY_DIR = REPO_ROOT / "deploy"
K8S_DIR = DEPLOY_DIR / "k8s"


def test_dockerfiles_exist():
    root_dockerfile = REPO_ROOT / "Dockerfile"
    deploy_dockerfile = DEPLOY_DIR / "Dockerfile"
    assert root_dockerfile.exists()
    assert deploy_dockerfile.exists()

    content = root_dockerfile.read_text(encoding="utf-8")
    assert "FROM python:3.11-slim" in content
    assert "USER appuser" in content
    assert "HEALTHCHECK" in content


def test_docker_compose_valid():
    compose_path = REPO_ROOT / "docker-compose.yml"
    assert compose_path.exists()
    data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    assert "services" in data
    assert "api" in data["services"]
    assert "swarm" in data["services"]
    assert "postgres" in data["services"]


def test_k8s_manifests_exist_and_parse():
    required_files = [
        "kustomization.yaml",
        "deployment.yaml",
        "service.yaml",
        "configmap.yaml",
        "pvc.yaml",
    ]
    for filename in required_files:
        f = K8S_DIR / filename
        assert f.exists(), f"Missing Kubernetes manifest: {filename}"
        docs = list(yaml.safe_load_all(f.read_text(encoding="utf-8")))
        assert len(docs) >= 1
        for doc in docs:
            assert "apiVersion" in doc
            assert "kind" in doc
            if doc["kind"] != "Kustomization":
                assert "metadata" in doc
            else:
                assert "resources" in doc


def test_kubectl_kustomize_compilation():
    """If kubectl is present on the machine, test that kustomize builds without error."""
    kubectl = shutil.which("kubectl")
    if not kubectl:
        pytest.skip("kubectl not installed on local PATH")

    res = subprocess.run(
        [kubectl, "kustomize", str(K8S_DIR)],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"kustomize failed: {res.stderr}"
    assert "kind: Deployment" in res.stdout
    assert "kind: Service" in res.stdout
    assert "kind: ConfigMap" in res.stdout
    assert "kind: PersistentVolumeClaim" in res.stdout
