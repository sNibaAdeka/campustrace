#!/usr/bin/env python3
"""Measure what the user actually waits for, across many universities.

The case allows 30 seconds from submitting a name to a useful result, and the
judges will type a university that is not in our demo. A number that only
covers the part of the pipeline after identity resolution would be flattering
and wrong, so this harness measures the whole thing from the outside, through
the public HTTP API, exactly as a browser does:

    submit -> search suggestion         (the user picks an institution)
    submit -> first useful photograph   (gallery can start rendering)
    submit -> complete profile          (all sections final)

Usage
-----
    # 1. start the server in another shell
    uvicorn app.main:app --port 8765

    # 2. run against a cold cache (this is the honest number). --refresh
    #    rebuilds the profile; for a truly cold run also start the server with
    #    an empty DATABASE_PATH, otherwise source responses come from the 24 h
    #    cache. The table reports cache hits per row either way.
    python3 scripts/benchmark.py --base http://127.0.0.1:8765 --refresh \
        --out docs/benchmark.json --markdown docs/benchmark.md

Nothing here writes into the README on its own. Copy the produced table in,
and always publish the date, the sample size and the failures alongside it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

import httpx


# A deliberately awkward sample: several countries and scripts, large and small
# institutions, two universities in one city, common abbreviations, and names
# that are ambiguous on purpose. Extend it rather than trimming it.
DEFAULT_SAMPLE = [
    "Nazarbayev University",
    "Astana IT University",          # same city as the previous one
    "Al-Farabi Kazakh National University",
    "University of Tartu",
    "МГУ",                            # Cyrillic abbreviation
    "MIT",                            # short acronym
    "University of Pennsylvania",
    "Universität Heidelberg",
    "Politecnico di Milano",
    "Université de Montréal",
    "National University of Singapore",
    "Universidad de Buenos Aires",
    "Uppsala universitet",
    "University of Cape Town",
    "Kyoto University",
]


async def one(client: httpx.AsyncClient, query: str, refresh: bool) -> dict:
    row: dict[str, object] = {"query": query}
    started = time.monotonic()
    try:
        search = await client.get("/api/search/suggest", params={"q": query}, timeout=30)
        search.raise_for_status()
        payload = search.json()
        results = payload.get("results") or []
        row["search_ms"] = int((time.monotonic() - started) * 1000)
        if not results:
            # An unreachable registry is not "the university does not exist".
            row["outcome"] = "source_error" if payload.get("warning") else "not_found"
            row["detail"] = payload.get("warning")
            return row
        chosen = results[0]
        row["resolved_to"] = chosen.get("name")
        row["ror_id"] = chosen.get("ror_id")

        params = {"refresh": "true"} if refresh else None
        profile_started = time.monotonic()

        async def preview() -> None:
            # The browser asks for the preview and the full profile at the same
            # moment; the first photo is on screen when the preview returns.
            try:
                reply = await client.get(f"/api/profiles/{chosen['ror_id']}/preview", timeout=30)
                if reply.status_code == 200 and reply.json().get("assets"):
                    row["first_photo_ms"] = row["search_ms"] + int((time.monotonic() - profile_started) * 1000)
                    row["preview_assets"] = len(reply.json()["assets"])
            except httpx.HTTPError:
                pass

        preview_task = asyncio.ensure_future(preview())
        response = await client.get(f"/api/profiles/{chosen['ror_id']}", params=params, timeout=60)
        await preview_task
        response.raise_for_status()
        profile = response.json()
    except httpx.HTTPError as exc:
        row["outcome"] = "error"
        row["detail"] = type(exc).__name__
        row["total_ms"] = int((time.monotonic() - started) * 1000)
        return row

    row["total_ms"] = int((time.monotonic() - started) * 1000)
    row["profile_ms"] = int((time.monotonic() - profile_started) * 1000)
    row.setdefault("first_photo_ms", None)
    if row["first_photo_ms"] is None and profile.get("assets"):
        row["first_photo_ms"] = row["total_ms"]  # no preview: photos arrive with the profile
    events = profile.get("source_events") or []
    # How much of this build came from the 24 h source cache. A "cold" number
    # is only cold when this is zero; the report states it per row.
    row["source_requests"] = len(events)
    row["source_cache_hits"] = sum(1 for e in events if e.get("outcome") == "cache")
    row["vision_checked"] = (profile.get("vision") or {}).get("checked", 0)
    row["assets"] = len(profile.get("assets") or [])
    row["coverage_sections"] = sum(1 for v in (profile.get("coverage") or {}).values() if v)
    row["unclassified"] = profile.get("unclassified_count", 0)
    row["status"] = profile.get("profile_status")
    row["from_cache"] = profile.get("from_cache")
    row["visual_check"] = (profile.get("vision") or {}).get("available", False)
    row["timings"] = profile.get("timings")
    row["outcome"] = "ok" if row["assets"] else "empty"
    return row


def percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def summarise(rows: list[dict]) -> dict:
    ok = [r for r in rows if r.get("outcome") == "ok"]
    totals = [r["total_ms"] for r in ok]
    firsts = [r["first_photo_ms"] for r in ok if r.get("first_photo_ms") is not None]
    return {
        "universities_attempted": len(rows),
        "universities_with_material": len(ok),
        "empty": sum(1 for r in rows if r.get("outcome") == "empty"),
        "not_found": sum(1 for r in rows if r.get("outcome") == "not_found"),
        "errors": sum(1 for r in rows if r.get("outcome") == "error"),
        "source_errors": sum(1 for r in rows if r.get("outcome") == "source_error"),
        "fully_cold_builds": sum(1 for r in ok if r.get("source_requests") and not r.get("source_cache_hits")),
        "partial_profiles": sum(1 for r in ok if r.get("status") == "partial"),
        "submit_to_complete_ms": {
            "p50": percentile(totals, 0.5), "p95": percentile(totals, 0.95),
            "max": max(totals) if totals else None,
            "over_30s": sum(1 for t in totals if t > 30_000),
        },
        "submit_to_first_photo_ms": {
            "p50": percentile(firsts, 0.5), "p95": percentile(firsts, 0.95),
            "max": max(firsts) if firsts else None,
        },
        "mean_assets": round(statistics.fmean([r["assets"] for r in ok]), 1) if ok else 0,
        "measured_at": time.strftime("%Y-%m-%d %H:%M:%S%z"),
    }


def markdown(rows: list[dict], summary: dict) -> str:
    lines = [
        "| Запрос | Определён как | Материалов | Разделов | submit→первое фото | submit→полный профиль | Из кэша источников | Vision | Статус |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        first = row.get("first_photo_ms")
        total = row.get("total_ms")
        lines.append(
            f"| {row['query']} | {row.get('resolved_to') or '—'} | {row.get('assets', '—')} | "
            f"{row.get('coverage_sections', '—')} | "
            f"{'—' if first is None else f'{first / 1000:.1f} с'} | "
            f"{'—' if total is None else f'{total / 1000:.1f} с'} | "
            f"{row.get('source_cache_hits', '—')}/{row.get('source_requests', '—')} | "
            f"{row.get('vision_checked', '—')} | "
            f"{row.get('status') or row.get('outcome')} |"
        )
    complete = summary["submit_to_complete_ms"]
    first = summary["submit_to_first_photo_ms"]
    lines += [
        "",
        f"Замер: {summary['measured_at']}. Вузов: {summary['universities_attempted']}, "
        f"с материалами: {summary['universities_with_material']}, "
        f"пустых: {summary['empty']}, не найдено в реестре: {summary['not_found']}, "
        f"ошибок: {summary['errors']}, недоступен реестр: {summary['source_errors']}, "
        f"неполных профилей: {summary['partial_profiles']}. Сборок без единого попадания в кэш источников: "
        f"{summary['fully_cold_builds']}. Стенд: {summary.get('base')}.",
        "",
        f"submit → первое фото: p50 {first['p50']} мс, p95 {first['p95']} мс.",
        f"submit → полный профиль: p50 {complete['p50']} мс, p95 {complete['p95']} мс, "
        f"максимум {complete['max']} мс, превысили 30 с: {complete['over_30s']}.",
    ]
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--refresh", action="store_true",
                        help="force a cold rebuild; without it you are timing the cache")
    parser.add_argument("--out", type=Path, help="write the raw JSON rows here")
    parser.add_argument("--markdown", type=Path, help="write a ready-to-paste table here")
    parser.add_argument("names", nargs="*", default=None)
    args = parser.parse_args()

    sample = args.names or DEFAULT_SAMPLE
    rows: list[dict] = []
    async with httpx.AsyncClient(base_url=args.base) as client:
        for name in sample:
            # Sequential on purpose: parallel runs would measure our own
            # contention against Wikimedia's rate limiter, not the user's wait.
            row = await one(client, name, args.refresh)
            rows.append(row)
            print(f"{row['query']:<42} {row.get('outcome'):<10} "
                  f"{row.get('total_ms', '—')} ms  assets={row.get('assets', '—')}", flush=True)

    summary = summarise(rows)
    summary["base"] = args.base
    summary["refresh"] = args.refresh
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2))
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown(rows, summary) + "\n")
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
