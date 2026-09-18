#!/usr/bin/env python3
"""Warm the profile cache on a fresh deployment.

Free hosting has no persistent disk, so every restart starts cold. This walks a
list of universities through the same public API a browser uses. With
``--passes 2`` or more it rebuilds each profile again after a pause: vision
verdicts are cached per photo, so every pass checks photographs the previous
one could not reach within the provider's per-minute quota.

    python3 scripts/prewarm.py --base https://<app>.onrender.com --passes 2

It prints what it did and never edits any report — timings measured here are
warm-cache numbers and must not be presented as cold ones.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import httpx

DEMO = [
    "Nazarbayev University", "Astana IT University", "Al-Farabi Kazakh National University",
    "Massachusetts Institute of Technology", "Kyoto University", "University of Tartu",
    "University of Pennsylvania", "University of Oxford",
]


async def warm(client: httpx.AsyncClient, name: str, refresh: bool) -> str:
    search = await client.get("/api/search/suggest", params={"q": name}, timeout=30)
    results = search.json().get("results") or []
    if not results:
        return f"{name:<40} not found ({search.json().get('warning') or 'no match'})"
    ror = results[0]["ror_id"]
    started = time.monotonic()
    reply = await client.get(f"/api/profiles/{ror}", params={"refresh": "true"} if refresh else None, timeout=90)
    if reply.status_code != 200:
        return f"{name:<40} HTTP {reply.status_code}"
    data = reply.json()
    vision = data.get("vision") or {}
    return (f"{name:<40} {len(data.get('assets') or []):>3} photos  "
            f"{time.monotonic() - started:5.1f} s  vision checked {vision.get('checked', 0)}"
            f"{' (quota hit)' if vision.get('rate_limited') else ''}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--passes", type=int, default=1)
    parser.add_argument("--pause", type=float, default=65, help="seconds between passes (vision quota window)")
    parser.add_argument("names", nargs="*")
    args = parser.parse_args()
    names = args.names or DEMO
    async with httpx.AsyncClient(base_url=args.base) as client:
        for number in range(args.passes):
            print(f"— pass {number + 1}/{args.passes}", flush=True)
            for name in names:
                try:
                    print(await warm(client, name, refresh=number > 0), flush=True)
                except httpx.HTTPError as exc:
                    print(f"{name:<40} error {type(exc).__name__}", flush=True)
            if number + 1 < args.passes:
                await asyncio.sleep(args.pause)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
