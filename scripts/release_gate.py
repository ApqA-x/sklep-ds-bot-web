#!/usr/bin/env python3
"""R26-11 (V26-26): гейты публикации publish.yml без двусмысленностей gh api.

ancestry    — предок ли кандидат HEAD(main): `git merge-base --is-ancestor`
              по реальному графу Git. gh api compare отдавал ahead/behind —
              relative-статус сравниваемых ref'ов, а не достижимость коммита.
conclusions — зелёный ли CI ровно на SHA кандидата: решает самый СВЕЖИЙ run
              каждого обязательного job'а (старый green не перекрывает свежий
              red rerun; in_progress/cancelled/missing — это не success).

Чистые функции (iter_runs/group_runs/latest_verdict) отделены от CLI, чтобы
юнит-тесты api/tests/test_release_artifacts.py не дёргали git и сеть.
Только stdlib, python>=3.10.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )


def check_ancestry(repo: Path, candidate: str) -> tuple[bool, str]:
    """(ok, пояснение): candidate — предок или сам HEAD репозитория repo?

    merge-base --is-ancestor считает равный коммит предком сам по себе,
    поэтому identical отдельно подтверждается сверкой полных sha.
    """
    verify = _git(repo, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}")
    if verify.returncode != 0:
        return False, f"unknown: SHA '{candidate}' — не коммит в {repo}"
    head = _git(repo, "rev-parse", "HEAD")
    if head.returncode != 0:
        return False, f"unknown: в {repo} нет HEAD"
    res = _git(repo, "merge-base", "--is-ancestor", candidate, "HEAD")
    if res.returncode == 0:
        if verify.stdout.strip() == head.stdout.strip():
            return True, "identical: кандидат == HEAD main"
        return True, "ancestor: кандидат лежит в истории main"
    if res.returncode == 1:
        back = _git(repo, "merge-base", "--is-ancestor", "HEAD", candidate)
        if back.returncode == 0:
            return False, "descendant: кандидат идёт ПОСЛЕ HEAD main — не публикуем"
        return False, "diverged: кандидат не достижим из main (чужая ветка) — не публикуем"
    return False, f"unknown: git merge-base --is-ancestor вернул {res.returncode}: {res.stderr.strip()}"


def cmd_ancestry(args: argparse.Namespace) -> int:
    ok, message = check_ancestry(Path(args.repo).resolve(), args.candidate)
    print(f"ancestry {args.candidate}: {'PASS' if ok else 'FAIL'} — {message}")
    return 0 if ok else 1


def iter_runs(payload: object) -> list[dict]:
    """check-runs из gh api --paginate (см. read_checks — формат входа).

    Принимает единый {"check_runs": [...]}, список страниц
    [{"check_runs": [...]}, ...] (это реальный вывод --paginate в файл)
    и голый список run'ов.
    """
    if isinstance(payload, dict):
        runs = payload.get("check_runs")
        return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []
    if isinstance(payload, list):
        out: list[dict] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            page = item.get("check_runs")
            if isinstance(page, list):
                out.extend(r for r in page if isinstance(r, dict))
            elif "name" in item:
                out.append(item)
        return out
    return []


def group_runs(runs: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for run in runs:
        grouped.setdefault(str(run.get("name", "")), []).append(run)
    return grouped


def _freshness(run: dict) -> str:
    # ISO-8601 сортируется лексикографически; у незавершённого run
    # completed_at null -> откат на started_at (свежий in_progress перекрывает
    # старый success, а не наоборот)
    return str(run.get("completed_at") or run.get("started_at") or "")


def latest_verdict(
    runs_by_name: dict[str, list[dict]], required: list[str]
) -> dict[str, tuple[bool, str]]:
    """По каждому обязательному имени: решает самый свежий run.

    PASS только если у него status == "completed" и conclusion == "success".
    Пустой required -> пустой вердикт (workflow фиксирует список имён, но
    функция не должна на нём падать).
    """
    verdicts: dict[str, tuple[bool, str]] = {}
    for name in required:
        runs = sorted(runs_by_name.get(name, []), key=_freshness, reverse=True)
        if not runs:
            verdicts[name] = (False, "no runs (missing)")
            continue
        fresh = runs[0]
        status = fresh.get("status")
        conclusion = fresh.get("conclusion")
        if status != "completed":
            verdicts[name] = (False, f"status {status}")
        elif conclusion != "success":
            verdicts[name] = (False, f"conclusion {conclusion}")
        else:
            verdicts[name] = (True, "conclusion success")
    return verdicts


def read_checks(path: Path) -> object:
    """JSON из `gh api .../check-runs --paginate > file`.

    Проверено живьём (gh 2.x, 27.09, f5e0ebb): --paginate пишет N документов
    подряд (по одному на страницу), json.loads на таком падает с "Extra data".
    Терпим оба формата: единый {"check_runs": [...]} и конкатенацию страниц
    (возвращается списком документов — iter_runs разбирает и его).
    """
    raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        docs: list[object] = []
        idx = 0
        while idx < len(raw):
            while idx < len(raw) and raw[idx].isspace():
                idx += 1
            if idx >= len(raw):
                break
            doc, idx = decoder.raw_decode(raw, idx)
            docs.append(doc)
        if not docs:
            raise json.JSONDecodeError("no JSON documents", raw, 0)
        if len(docs) == 1:
            return docs[0]
        return docs


def cmd_conclusions(args: argparse.Namespace) -> int:
    try:
        payload = read_checks(Path(args.checks))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"conclusions: не читается {args.checks}: {exc}", file=sys.stderr)
        return 2
    required = [name.strip() for name in args.required.split(",") if name.strip()]
    if not required:
        print("conclusions: --required пуст — нечего проверять", file=sys.stderr)
        return 2
    verdicts = latest_verdict(group_runs(iter_runs(payload)), required)
    failed: list[str] = []
    for name, (ok, reason) in verdicts.items():
        print(f"{'PASS' if ok else 'FAIL'} {name}: {reason}")
        if not ok:
            failed.append(name)
    if failed:
        print(
            f"conclusions: CI не зелёный на этом SHA ({', '.join(failed)}) — публикация запрещена",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R26-11 release gates (ancestry / conclusions)")
    sub = parser.add_subparsers(dest="command", required=True)

    anc = sub.add_parser("ancestry", help="кандидат — предок/сам HEAD main (git merge-base)")
    anc.add_argument("--candidate", required=True, help="SHA из тега")
    anc.add_argument("--repo", default=".", help="каталог git-репозитория (по умолчанию cwd)")
    anc.set_defaults(handler=cmd_ancestry)

    con = sub.add_parser("conclusions", help="свежие conclusion'ы CI на SHA кандидата")
    con.add_argument("--checks", required=True, help="JSON ответа gh api .../check-runs")
    con.add_argument("--required", required=True, help="обязательные имена job'ов через запятую")
    con.set_defaults(handler=cmd_conclusions)

    args = parser.parse_args(argv)
    result: int = args.handler(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
