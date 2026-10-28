"""Review and approve taxonomy merges.

    python -m app.merge_cli plan [--json] [--window-days N]
        Read-only dry run: compute the domain/topic merge map from live data and
        print it. Runs in a READ ONLY transaction — it cannot write.

    python -m app.merge_cli pending
        List the proposals /jobs/topic-merge has stored (status='proposed').

    python -m app.merge_cli approve --kind topic ALIAS CANONICAL --by EMAIL --yes
    python -m app.merge_cli reject --kind topic ALIAS --by EMAIL --yes
        Record one decision. Only 'approved' rows take effect (readers resolve
        names through them); session history is never rewritten, so reverting
        is just `reject` on the same alias.

    python -m app.merge_cli approve-plan (PLAN.json | --live) --by EMAIL
            [--kind domain|topic|all] [--only-reason R ...] [--window-days N] [--yes]
        Approve a batch. PLAN.json is `plan --json` output, edited down to the
        merges you accept; --live builds the plan in-process from the DB
        instead (no file — what the Cloud Run admin job uses). --only-reason
        (repeatable) keeps proposals whose reasons include any given value,
        e.g. `name`. Without --yes it prints exactly what would be approved or
        skipped and writes nothing.

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

Decision = tuple[str, str, str, list]  # (kind, alias, canonical, reasons)


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


async def _load_kind(cur, kind: str) -> tuple[dict, set]:
    current = await load_alias_map(cur, kind)
    await cur.execute(f"SELECT name FROM {_TABLES[kind]}")
    return current, {r[0] for r in await cur.fetchall()}


async def _apply(cur, decisions: list[Decision], by: str, write: bool) -> tuple[int, int]:
    """Run every decision through check_decision, in order, against an alias
    map that already includes the earlier approvals in this batch — so a dry run
    reports exactly what the write would do. Prints one line per merge."""
    state: dict[str, tuple[dict, set]] = {}
    approved = skipped = 0
    verb = "approved" if write else "would approve"
    for kind, alias, canonical, reasons in decisions:
        if kind in KINDS and kind not in state:
            state[kind] = await _load_kind(cur, kind)
        current, known = state.get(kind, ({}, set()))
        problem = check_decision(kind, alias, canonical, current, known)
        if problem:
            print(f"{'skip' if write else 'would skip'} {kind} {alias} -> {canonical}: {problem}")
            skipped += 1
            continue
        if write:
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
        current[alias] = canonical
        approved += 1
        tag = f"  [{', '.join(reasons)}]" if reasons else ""
        print(f"{verb} {kind} {alias} -> {canonical}{tag}")
    return approved, skipped


async def _approve(decisions: list[Decision], by: str, write: bool = True) -> int:
    async with await _connect(read_only=not write) as conn:
        async with conn.cursor() as cur:
            approved, skipped = await _apply(cur, decisions, by, write)
        if write:
            await conn.commit()
    return _summary(approved, skipped, write)


async def _approve_live(args, write: bool) -> int:
    async with await _connect(read_only=not write) as conn:
        async with conn.cursor() as cur:
            plan = await build_merge_plan(cur, args.window_days)
            decisions = plan_decisions(plan, args.kind, args.only_reason)
            approved, skipped = await _apply(cur, decisions, args.by, write)
        if write:
            await conn.commit()
    return _summary(approved, skipped, write)


def _summary(approved: int, skipped: int, write: bool) -> int:
    if write:
        print(f"{approved} approved, {skipped} skipped")
        return 1 if skipped else 0
    print(f"dry run: {approved} would be approved, {skipped} skipped — nothing "
          "written; re-run with --yes to apply")
    return 0


def plan_decisions(
    plan: dict, kind: str = "all", only_reasons: list[str] | None = None
) -> list[Decision]:
    """Flatten a plan ({kind: [proposal]}) into decisions, keeping `kind` (or
    every kind for 'all') and, when `only_reasons` is given, only proposals
    whose reasons include at least one of them."""
    kinds = KINDS if kind == "all" else (kind,)
    wanted = set(only_reasons or ())
    return [
        (k, r["alias"], r["canonical"], list(r.get("reasons") or ()))
        for k in kinds
        for r in plan.get(k) or []
        if not wanted or wanted & set(r.get("reasons") or ())
    ]


def _read_plan(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


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
    p.add_argument("plan_file", nargs="?")
    p.add_argument("--live", action="store_true",
                   help="build the plan from the DB instead of reading PLAN.json")
    p.add_argument("--kind", choices=(*KINDS, "all"), default="all")
    p.add_argument("--only-reason", action="append", metavar="REASON",
                   help="keep proposals whose reasons include REASON (repeatable)")
    p.add_argument("--window-days", type=int, default=MERGE_WINDOW_DAYS)
    p.add_argument("--by", required=True)
    p.add_argument("--yes", action="store_true",
                   help="write; without it, print what would be approved")

    args = ap.parse_args(argv)
    if args.cmd == "plan":
        return asyncio.run(_plan(args))
    if args.cmd == "pending":
        return asyncio.run(_pending(args))
    if "@" not in args.by:
        ap.error("--by must be the approver's email")
    if args.cmd == "approve-plan":
        if args.live == bool(args.plan_file):
            ap.error("approve-plan takes exactly one of PLAN.json or --live")
        if args.live:
            return asyncio.run(_approve_live(args, write=args.yes))
        decisions = plan_decisions(_read_plan(args.plan_file), args.kind, args.only_reason)
        return asyncio.run(_approve(decisions, args.by, write=args.yes))
    if not args.yes:
        print("refusing to write without --yes (review `plan` output first)", file=sys.stderr)
        return 2
    if args.cmd == "approve":
        return asyncio.run(_approve([(args.kind, args.alias, args.canonical, [])], args.by))
    return asyncio.run(_reject(args.kind, args.alias, args.by))


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
