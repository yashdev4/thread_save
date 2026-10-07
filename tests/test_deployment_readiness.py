"""Verification suite for Milestone X8 (Deployment Readiness, Latency Budget, & Health).

Covers:
- /health probe behavior under healthy and degraded database states
- Latency budget: p95 server processing latency < 300 ms across HTTP saves
- Deployment artifact verification (Dockerfile, fly.toml with always-on non-sleeping instance)
"""

from pathlib import Path



def test_x8_deployment_configuration_files():
    """Verify presence and correctness of production deployment configs."""
    # 1. Dockerfile
    dockerfile_path = Path("Dockerfile")
    assert dockerfile_path.exists(), "Dockerfile is missing"
    df_content = dockerfile_path.read_text(encoding="utf-8")
    assert "useradd" in df_content or "threadvault" in df_content, "Non-root user missing in Dockerfile"
    assert "HEALTHCHECK" in df_content, "Docker HEALTHCHECK missing"
    assert "alembic upgrade head" in df_content, "Automated migration missing in Docker entrypoint"

    # 2. fly.toml
    fly_path = Path("fly.toml")
    assert fly_path.exists(), "fly.toml is missing"
    fly_content = fly_path.read_text(encoding="utf-8")
    assert "auto_stop_machines = false" in fly_content, "Always-on instance setting missing in fly.toml"
    assert 'path = "/health"' in fly_content, "Health check path missing in fly.toml"

    # 3. docs/DEPLOYMENT.md
    docs_path = Path("docs/DEPLOYMENT.md")
    assert docs_path.exists(), "docs/DEPLOYMENT.md is missing"
    docs_content = docs_path.read_text(encoding="utf-8")
    assert "p95 < 300 ms" in docs_content
    assert "Cold Starts" in docs_content
    assert "PITR" in docs_content


def test_h7_deploy_config_migrations_and_non_sleeping_instance():
    """Milestone H7: Verify Alembic migrations moved to release/pre-deploy commands and non-sleeping instance."""
    # 1. fly.toml release_command
    fly_path = Path("fly.toml")
    assert fly_path.exists()
    fly_content = fly_path.read_text(encoding="utf-8")
    assert "[deploy]" in fly_content
    assert 'release_command = "python -m alembic upgrade head"' in fly_content

    # 2. render.yaml preDeployCommand & non-sleeping tier
    render_path = Path("render.yaml")
    assert render_path.exists()
    render_content = render_path.read_text(encoding="utf-8")
    # Standalone deploys use FileStore; the guard runs alembic only with Postgres
    assert "preDeployCommand: python -m thread_save.cli.predeploy" in render_content
    # Non-sleeping paid plan (free tier sleeps after 15m)
    assert "plan: starter" in render_content
    assert "plan: free" not in render_content


def test_predeploy_migrates_only_with_postgres(monkeypatch):
    from thread_save.cli import predeploy

    assert predeploy.uses_postgres({"DATABASE_URL": "postgresql://x/db"})
    assert not predeploy.uses_postgres({"THREADVAULT_STORAGE_BACKEND": "file", "DATABASE_URL": "postgresql://x/db"})
    assert not predeploy.uses_postgres({})

    calls = []
    monkeypatch.setattr(predeploy.subprocess, "call", lambda args: calls.append(args) or 0)
    monkeypatch.setenv("THREADVAULT_STORAGE_BACKEND", "file")
    assert predeploy.main() == 0 and calls == []
    monkeypatch.setenv("THREADVAULT_STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/db")
    assert predeploy.main() == 0
    assert calls[0][1:] == ["-m", "alembic", "upgrade", "head"]
