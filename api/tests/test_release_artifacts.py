"""T15: release-артефакты веба как контракт (CI01-CI06 механика на файлах).

Главный ловушечный сценарий, который здесь запрещён: декоративный lock +
ranges-установка в Dockerfile, и publish-gate, требующий несуществующий
check (блокирует все релизы) или, наоборот, не требующий ничего (CI03).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
PUBLISH = yaml.safe_load((ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8"))
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
RUNTIME_LOCK = (ROOT / "api/requirements.lock").read_text(encoding="utf-8")
TEST_LOCK = (ROOT / "api/requirements-test.lock").read_text(encoding="utf-8")
REQUIREMENTS = (ROOT / "api/requirements.txt").read_text(encoding="utf-8")
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8")


def triggers(workflow: dict) -> dict:
    """YAML 1.1 превращает ключ `on:` в True — читаем оба варианта."""
    return workflow.get("on") or workflow.get(True) or {}


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock_names(lock: str) -> set[str]:
    return {_normalize(m) for m in re.findall(r"^([A-Za-z0-9_.\-]+)==", lock, re.MULTILINE)}


def _job_display_name(job_id: str, job: dict) -> str:
    return job.get("name", job_id)


def test_dockerfile_installs_hashed_lock_not_ranges() -> None:
    """CI01/p.1: образ ставит только requirements.lock с проверкой sha256."""
    assert "--require-hashes" in DOCKERFILE
    assert "requirements.lock" in DOCKERFILE
    assert not re.search(r"pip install -r api/requirements\.txt", DOCKERFILE)
    assert "npm ci" in DOCKERFILE  # UI — тоже строго по package-lock
    assert "USER 10001:10001" in DOCKERFILE  # п.9


def test_runtime_lock_covers_requirements() -> None:
    """lock реальный: каждый top-level пакет requirements.txt зафиксирован."""
    specs = re.findall(r"^([A-Za-z0-9_.\-]+)", REQUIREMENTS, re.MULTILINE)
    locked = _lock_names(RUNTIME_LOCK)
    missing = {s for s in specs if _normalize(s) not in locked}
    assert not missing, f"requirements.lock не покрывает: {missing}"
    # тестовый лок — надмножество рантайм-лока
    assert locked <= _lock_names(TEST_LOCK)


def test_dockerignore_excludes_local_env() -> None:
    lines = {l.strip() for l in DOCKERIGNORE.splitlines()}
    assert ".env" in lines and ".env.*" in lines


def test_ci_required_jobs_install_locked_and_run_integration() -> None:
    """п.4: unit/integration/e2e — обязательные job'ы без continue-on-error."""
    installs = " ".join(s.get("run", "") for s in CI["jobs"]["api"]["steps"])
    assert "--require-hashes" in installs and "requirements-test.lock" in installs
    for job_id in ("api", "integration", "ui", "ui-e2e"):
        assert "continue-on-error" not in CI["jobs"][job_id]
    assert "-m integration" in yaml.dump(CI["jobs"]["integration"])
    assert "npx playwright test" in yaml.dump(CI["jobs"]["ui-e2e"])


def test_publish_gate_requires_exactly_the_real_ci_job_names() -> None:
    """п.6/CI03: gate требует существующие check'и и не имеет обхода."""
    ci_names = {_job_display_name(jid, j) for jid, j in CI["jobs"].items()}
    gate_run = PUBLISH["jobs"]["gate"]["steps"][-1]["run"]
    loops = re.findall(r"for REQUIRED in ([^;]+);", gate_run)
    assert loops, "gate не перечисляет обязательные checks"
    wanted = set(loops[0].split())
    unknown = wanted - ci_names
    assert not unknown, f"gate ждёт несуществующие checks: {unknown}"
    # dispatch не обходит gate: build напрямую зависит только от gate
    def as_list(v):
        return v if isinstance(v, list) else [v]

    assert as_list(PUBLISH["jobs"]["build"]["needs"]) == ["gate"]
    assert as_list(PUBLISH["jobs"]["smoke"]["needs"]) == ["gate", "build"]
    # dispatch возможен только через именованный тег, который проходит тот же gate
    trig = triggers(PUBLISH)
    assert "workflow_dispatch" in trig
    assert trig["workflow_dispatch"]["inputs"]["tag"]["required"] is True


def test_e2e_assets_present() -> None:
    """p.3: браузерный слой — конфиг+спек+скрипт+зависимость в локе."""
    ui = ROOT / "ui"
    assert (ui / "playwright.config.ts").is_file()
    spec = (ui / "e2e/app.spec.ts").read_text(encoding="utf-8")
    assert "/api/auth/whoami" in spec and "leaderboard" in spec
    pkg = (ui / "package.json").read_text(encoding="utf-8")
    assert "@playwright/test" in pkg and '"test"' in pkg
    lock = (ui / "package-lock.json").read_text(encoding="utf-8")
    assert "@playwright/test" in lock
