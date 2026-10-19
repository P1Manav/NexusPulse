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


def test_dockerfile_exists():
    root_dockerfile = REPO_ROOT / "Dockerfile"
    assert root_dockerfile.exists()

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


def test_render_blueprint_uses_persistent_single_instance_sqlite():
    render_path = REPO_ROOT / "render.yaml"
    assert render_path.exists()
    data = yaml.safe_load(render_path.read_text(encoding="utf-8"))
    service = data["services"][0]
    env_vars = {item["key"]: item["value"] for item in service["envVars"]}

    assert service["type"] == "web"
    assert service["runtime"] == "docker"
    assert service["dockerfilePath"] == "./Dockerfile"
    assert service["healthCheckPath"] == "/readyz"
    assert service["numInstances"] == 1
    assert service["disk"]["mountPath"] == "/app/data"
    assert env_vars["SQLITE_PATH"] == "/app/data/board.db"


def test_k8s_manifests_exist_and_parse():
    required_files = [
        "kustomization.yaml",
        "deployment.yaml",
        "service.yaml",
        "configmap.yaml",
        "pvc.yaml",
        "swarm-job.yaml",
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

    # Deployment specific single-replica checks
    dep_content = yaml.safe_load((K8S_DIR / "deployment.yaml").read_text(encoding="utf-8"))
    assert dep_content["spec"]["replicas"] == 1
    assert dep_content["spec"]["strategy"]["type"] == "Recreate"


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
    assert "kind: Job" in res.stdout
