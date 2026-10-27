"""Review and approve taxonomy merges.

    python -m app.merge_cli plan [--json] [--window-days N]
        Read-only dry run: compute the domain/topic merge map from live data and
        print it. Runs in a READ ONLY transaction — it cannot write.

    python -m app.merge_cli pending
        List the proposals /jobs/topic-merge has stored (status='proposed').

    python -m app.merge_cli approve --kind topic ALIAS CANONICAL --by EMAIL --yes
    python -m app.merge_cli approve-plan PLAN.json --by EMAIL --yes
    python -m app.merge_cli reject --kind topic ALIAS --by EMAIL --yes
        Record a decision. Only 'approved' rows take effect (readers resolve
        names through them); session history is never rewritten, so reverting
        is just `reject` on the same alias. PLAN.json is `plan --json` output,
        edited down to the merges you accept.

Needs CLAUSTRUM_DB_URL. Writes refuse to run without --yes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import psycopg

from app.aliases import load_alias_map
from app.merge_plan import MERGE_WINDOW_DAYS, build_merge_plan, format_plan

KINDS = ("topic", "domain")
_TABLES = {"topic": "topics", "domain": "domains"}


def check_decision(
    kind: str, alias: str, canonical: str, current: dict, known: set
) -> str | None:
    """Why an approval would be unsafe, or None. `current` is the live alias map
    (legacy parents + approved aliases); `known` the names in the taxonomy
    table. Pure, so the guard is testable."""
    if kind not in KINDS:
        return f"kind must be one of {KINDS}"
    if not alias or not canonical or alias == canonical:
        return "alias and canonical must be distinct, non-empty names"
    if canonical not in known:
        return f"'{canonical}' is not in the {kind} taxonomy — a canonical must be real"
    others = {a: c for a, c in current.items() if a != alias}
    node, seen = canonical, set()
    while node is not None and node not in seen:
        if node == alias:
            return f"'{canonical}' already resolves through '{alias}' — approving would loop"
        seen.add(node)
        node = others.get(node)
    return None


async def _connect(read_only: bool):
    conn = await psycopg.AsyncConnection.connect(os.environ["CLAUSTRUM_DB_URL"])
    if read_only:
        await conn.set_read_only(True)
    return conn


async def _plan(args) -> int:
    async with await _connect(read_only=True) as conn:
        async with conn.cursor() as cur:
            plan = await build_merge_plan(cur, args.window_days)
    if args.json:
        print(json.dumps(plan, indent=2, default=list))
    else:
        print(format_plan(plan))
    return 0


async def _pending(_args) -> int:
    async with await _connect(read_only=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT kind, alias, canonical, score, reasons FROM taxonomy_aliases "
                "WHERE status = 'proposed' ORDER BY kind, score DESC, alias"
            )
            rows = await cur.fetchall()
    for kind, alias, canonical, score, reasons in rows:
        print(f"{kind:6} {alias} -> {canonical}  score={score:.2f}  {reasons}")
    print(f"{len(rows)} pending")
    return 0


async def _approve(decisions: list[tuple], by: str) -> int:
    skipped = 0
    async with await _connect(read_only=False) as conn:
        async with conn.cursor() as cur:
            for kind, alias, canonical in decisions:
                current = await load_alias_map(cur, kind)
                await cur.execute(f"SELECT name FROM {_TABLES[kind]}")
                known = {r[0] for r in await cur.fetchall()}
                problem = check_decision(kind, alias, canonical, current, known)
                if problem:
                    print(f"skip {kind} {alias}: {problem}", file=sys.stderr)
                    skipped += 1
                    continue
                await cur.execute(
                    """
                    INSERT INTO taxonomy_aliases
                        (kind, alias, canonical, status, decided_at, decided_by)
                    VALUES (%(k)s, %(a)s, %(c)s, 'approved', now(), %(by)s)
                    ON CONFLICT (kind, alias) DO UPDATE
                        SET canonical = EXCLUDED.canonical,
                            status = 'approved',
                            decided_at = now(),
                            decided_by = EXCLUDED.decided_by
                    """,
                    {"k": kind, "a": alias, "c": canonical, "by": by},
                )
                print(f"approved {kind} {alias} -> {canonical}")
        await conn.commit()
    return 1 if skipped else 0


def _plan_decisions(path: str) -> list[tuple]:
    with open(path) as f:
        plan = json.load(f)
    return [
        (kind, r["alias"], r["canonical"])
        for kind in KINDS
        for r in plan.get(kind) or []
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.merge_cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan")
    p.add_argument("--json", action="store_true")
    p.add_argument("--window-days", type=int, default=MERGE_WINDOW_DAYS)
    sub.add_parser("pending")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("--kind", choices=KINDS, required=True)
        p.add_argument("alias")
        if name == "approve":
            p.add_argument("canonical")
        p.add_argument("--by", required=True)
        p.add_argument("--yes", action="store_true")
    p = sub.add_parser("approve-plan")
    p.add_argument("plan_file")
    p.add_argument("--by", required=True)
    p.add_argument("--yes", action="store_true")

    args = ap.parse_args(argv)
    if args.cmd == "plan":
        return asyncio.run(_plan(args))
    if args.cmd == "pending":
        return asyncio.run(_pending(args))
    if not args.yes:
        print("refusing to write without --yes (review `plan` output first)", file=sys.stderr)
        return 2
    if args.cmd == "approve":
        return asyncio.run(_approve([(args.kind, args.alias, args.canonical)], args.by))
    if args.cmd == "reject":
        return asyncio.run(_reject(args.kind, args.alias, args.by))
    return asyncio.run(_approve(_plan_decisions(args.plan_file), args.by))


async def _reject(kind: str, alias: str, by: str) -> int:
    async with await _connect(read_only=False) as conn:
        cur = await conn.execute(
            """
            UPDATE taxonomy_aliases
            SET status = 'rejected', decided_at = now(), decided_by = %(by)s
            WHERE kind = %(k)s AND alias = %(a)s
            """,
            {"k": kind, "a": alias, "by": by},
        )
        if cur.rowcount == 0:
            print(f"no proposal for {kind} '{alias}'", file=sys.stderr)
            return 1
        await conn.commit()
    print(f"rejected {kind} {alias}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
