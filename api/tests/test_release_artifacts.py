"""T15: release-артефакты веба как контракт (CI01-CI06 механика на файлах).

Главный ловушечный сценарий, который здесь запрещён: декоративный lock +
ranges-установка в Dockerfile, и publish-gate, требующий несуществующий
check (блокирует все релизы) или, наоборот, не требующий ничего (CI03).

R26-11 (V26-26/V26-27/V26-28): gate'ы publish.yml — граф Git и свежие
conclusion'ы через scripts/release_gate.py; публичные теги двигает promote
после smoke кандидата; npm test не зависит от shell-glob.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
PUBLISH = yaml.safe_load((ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8"))
PUBLISH_TEXT = (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
GATE_SCRIPT_TEXT = (ROOT / "scripts" / "release_gate.py").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
RUNTIME_LOCK = (ROOT / "api/requirements.lock").read_text(encoding="utf-8")
TEST_LOCK = (ROOT / "api/requirements-test.lock").read_text(encoding="utf-8")
REQUIREMENTS = (ROOT / "api/requirements.txt").read_text(encoding="utf-8")
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8")

SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import release_gate  # noqa: E402  # чистые функции гейдов — без git и сети


def triggers(workflow: dict) -> dict:
    """YAML 1.1 превращает ключ `on:` в True — читаем оба варианта."""
    return workflow.get("on") or workflow.get(True) or {}


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock_names(lock: str) -> set[str]:
    return {_normalize(m) for m in re.findall(r"^([A-Za-z0-9_.\-]+)==", lock, re.MULTILINE)}


def _job_display_name(job_id: str, job: dict) -> str:
    return job.get("name", job_id)


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _step_run(job: dict, marker: str) -> str:
    for step in job["steps"]:
        if marker in step.get("run", ""):
            return step["run"]
    raise AssertionError(f"шаг с '{marker}' не найден")


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
    """п.6/CI03 (R26-11): gate требует существующие check'и и не имеет обхода.

    Список обязательных job'ов — один аргумент --required шага conclusions;
    он обязан совпадать с display-имёнами jobs ci.yml (api, integration, ui,
    ui-e2e) — это же список required-checks защиты main.
    """
    ci_names = {_job_display_name(jid, j) for jid, j in CI["jobs"].items()}
    conclusions_run = _step_run(PUBLISH["jobs"]["gate"], "release_gate.py conclusions")
    m = re.search(r"--required\s+([A-Za-z0-9._-]+(?:,[A-Za-z0-9._-]+)*)", conclusions_run)
    assert m, "шаг conclusions не перечисляет обязательные checks через --required"
    wanted = set(m.group(1).split(","))
    assert wanted == ci_names, (
        f"gate ждёт {sorted(wanted)}, а ci.yml публикует checks {sorted(ci_names)}"
    )
    # dispatch не обходит gate: build напрямую зависит только от gate,
    # смоук — от build, promote — от смоука (R26-11/V26-27)
    assert _as_list(PUBLISH["jobs"]["build"]["needs"]) == ["gate"]
    assert _as_list(PUBLISH["jobs"]["smoke"]["needs"]) == ["gate", "build"]
    assert {"build", "smoke"} <= set(_as_list(PUBLISH["jobs"]["promote"]["needs"]))
    # dispatch возможен только через именованный тег, который проходит тот же gate
    trig = triggers(PUBLISH)
    assert "workflow_dispatch" in trig
    assert trig["workflow_dispatch"]["inputs"]["tag"]["required"] is True


def test_publish_gate_uses_git_ancestry_not_gh_compare() -> None:
    """V26-26: достижимость из main — по графу Git, gh api compare убран."""
    assert "compare/" not in PUBLISH_TEXT
    assert "release_gate.py ancestry" in PUBLISH_TEXT
    assert "--is-ancestor" in GATE_SCRIPT_TEXT


def _fixture_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # author/committer только через env — глобальный git-конфиг не затрагивается
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "r26-11-fixture",
        "GIT_AUTHOR_EMAIL": "r26-11-fixture@example.invalid",
        "GIT_COMMITTER_NAME": "r26-11-fixture",
        "GIT_COMMITTER_EMAIL": "r26-11-fixture@example.invalid",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    )


def _fixture_commit(repo: Path, message: str) -> str:
    (repo / "f.txt").write_text(message, encoding="utf-8")
    _fixture_git(repo, "add", "f.txt")
    _fixture_git(repo, "commit", "-m", message)
    return _fixture_git(repo, "rev-parse", "HEAD").stdout.strip()


def _ancestry(repo: Path, candidate: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "release_gate.py"),
            "ancestry",
            "--candidate",
            candidate,
            "--repo",
            str(repo),
        ],
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git не доступен")
def test_release_gate_ancestry_scenarios() -> None:
    """V26-26: ancestry на временном репозитории — предок/равный => 0,
    descendant/diverged/неизвестный SHA => 1 (случай печатается в вывод)."""
    repo = Path(tempfile.mkdtemp(prefix="r26-11-ancestry-"))
    try:
        _fixture_git(repo, "init", "-b", "main")
        ancestor = _fixture_commit(repo, "A")
        head = _fixture_commit(repo, "B")

        res = _ancestry(repo, ancestor)
        assert res.returncode == 0, res.stdout + res.stderr
        assert "ancestor" in res.stdout

        res = _ancestry(repo, head)
        assert res.returncode == 0, res.stdout + res.stderr
        assert "identical" in res.stdout

        # descendant: кандидат впереди HEAD main (коммит C на side-ветке)
        _fixture_git(repo, "checkout", "-b", "side")
        descendant = _fixture_commit(repo, "C")
        _fixture_git(repo, "checkout", "main")
        res = _ancestry(repo, descendant)
        assert res.returncode == 1
        assert "descendant" in res.stdout

        # diverged: main уходит вперёд, кандидат остаётся в ответвлении
        _fixture_commit(repo, "E")
        res = _ancestry(repo, descendant)
        assert res.returncode == 1
        assert "diverged" in res.stdout

        res = _ancestry(repo, "0" * 40)
        assert res.returncode == 1
        assert "unknown" in res.stdout
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def _check_run(
    name: str,
    status: str,
    conclusion: str | None = None,
    started: str = "2026-09-01T00:00:00Z",
    completed: str | None = None,
) -> dict:
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "started_at": started,
        "completed_at": completed,
    }


def _verdict(runs: list[dict], name: str = "api") -> tuple[bool, str]:
    return release_gate.latest_verdict(release_gate.group_runs(runs), [name])[name]


def test_release_gate_conclusions_fresh_run_decides() -> None:
    """V26-26 exact-SHA: решает самый свежий run обязательного job'а.

    Старый green не перекрывает свежий red rerun и наоборот; in_progress,
    cancelled и отсутствие run'а — не success; пустой required — пустой
    вердикт (workflow фиксирует 4 имени, но функция не должна падать).
    """
    ok, reason = _verdict(
        [
            _check_run("api", "completed", "success", completed="2026-09-01T10:00:00Z"),
            _check_run("api", "completed", "failure",
                       started="2026-09-02T09:00:00Z", completed="2026-09-02T10:00:00Z"),
        ]
    )
    assert not ok and reason == "conclusion failure"

    ok, reason = _verdict(
        [
            _check_run("api", "completed", "failure", completed="2026-09-01T10:00:00Z"),
            _check_run("api", "completed", "success",
                       started="2026-09-02T09:00:00Z", completed="2026-09-02T10:00:00Z"),
        ]
    )
    assert ok and reason == "conclusion success"

    ok, reason = _verdict([_check_run("api", "in_progress")])
    assert not ok and reason == "status in_progress"

    ok, reason = _verdict([_check_run("api", "completed", "cancelled",
                                      completed="2026-09-01T10:00:00Z")])
    assert not ok and reason == "conclusion cancelled"

    ok, reason = _verdict([_check_run("ui", "completed", "success")], name="api")
    assert not ok and reason == "no runs (missing)"

    assert release_gate.latest_verdict({}, []) == {}


def test_release_gate_conclusions_handles_paginated_join(tmp_path: Path) -> None:
    """V26-26: gh api --paginate > file пишет N документов подряд (живая
    проверка gh 2.x на f5e0ebb), а не один склеенный {"check_runs": [...]};
    read_checks обязан терпеть оба формата + единый документ + голый список."""
    api_ok = _check_run("api", "completed", "success", completed="2026-09-01T10:00:00Z")
    ui_bad = _check_run("ui", "completed", "failure", completed="2026-09-01T11:00:00Z")
    joined = release_gate.iter_runs({"total_count": 2, "check_runs": [api_ok, ui_bad]})
    pages = release_gate.iter_runs(
        [{"check_runs": [api_ok]}, {"check_runs": [ui_bad]}]
    )
    expected = {
        "api": (True, "conclusion success"),
        "ui": (False, "conclusion failure"),
    }
    assert release_gate.latest_verdict(release_gate.group_runs(joined), ["api", "ui"]) == expected
    assert release_gate.latest_verdict(release_gate.group_runs(pages), ["api", "ui"]) == expected

    # конкатенация двух страниц как реальный вывод gh --paginate
    concat = tmp_path / "checks.json"
    concat.write_text(
        json.dumps({"total_count": 1, "check_runs": [api_ok]})
        + "\n"
        + json.dumps({"total_count": 1, "check_runs": [ui_bad]}),
        encoding="utf-8",
    )
    assert release_gate.latest_verdict(
        release_gate.group_runs(release_gate.iter_runs(release_gate.read_checks(concat))),
        ["api", "ui"],
    ) == expected
    single = tmp_path / "single.json"
    single.write_text(json.dumps({"check_runs": [api_ok]}), encoding="utf-8")
    assert release_gate.iter_runs(release_gate.read_checks(single)) == [api_ok]


def test_release_promotion_only_after_smoke() -> None:
    """R26-11/V26-27: build пушит только кандидат ci-<SHA>, а
    version/minor/latest навешивает promote после smoke того же digest.

    Перетегирование — серверное (imagetools create по digest), без пересборки;
    workflow ничего не удаляет из registry.
    """
    promote = PUBLISH["jobs"]["promote"]
    promote_run = _step_run(promote, "imagetools create")
    assert re.search(r"IMAGE_LC@\"?\$\{?DIGEST", promote_run), (
        "promote обязан создавать релизные теги из digest кандидата ($IMAGE_LC@$DIGEST)"
    )
    assert "ci-" in promote_run, "digest для promote берётся из кандидатного ci-тега"

    build_dump = yaml.dump(PUBLISH["jobs"]["build"])
    assert "ci-" in build_dump
    assert "outputs.version" not in build_dump, "build больше не тегит release-версией"
    assert "latest" not in build_dump
    smoke_dump = yaml.dump(PUBLISH["jobs"]["smoke"])
    assert "ci-" in smoke_dump and "@$DIGEST" in smoke_dump, (
        "smoke проверяет именно digest кандидатного ci-тега"
    )

    for banned in ("DELETE", "delete-package", "untagged"):
        assert banned not in PUBLISH_TEXT, f"publish.yml не должен ничего удалять: {banned}"


def test_npm_test_runner_without_shell_glob() -> None:
    """V26-28: npm test = node scripts/run-node-tests.mjs (обход без glob —
    на Windows cmd кавычки не снимались, был `tests 0` при exit 0).

    Нулевой отбор обязан падать, а живой прогон под node даёт все UI-тесты
    (приёмка: не меньше 10).
    """
    pkg = json.loads((ROOT / "ui" / "package.json").read_text(encoding="utf-8"))
    assert pkg["scripts"]["test"] == "node scripts/run-node-tests.mjs"
    runner = ROOT / "ui" / "scripts" / "run-node-tests.mjs"
    assert runner.is_file()
    text = runner.read_text(encoding="utf-8")
    assert "R26-11: no test files selected" in text and "process.exit(1)" in text

    node = shutil.which("node")
    if node is None:
        pytest.skip("node не доступен")
    res = subprocess.run(
        [node, "scripts/run-node-tests.mjs"],
        cwd=ROOT / "ui",
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    m = re.search(r"tests\s+(\d+)", res.stdout + res.stderr)
    assert m, f"нет счётчика тестов в выводе node --test:\n{res.stdout}\n{res.stderr}"
    assert int(m.group(1)) >= 10


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
