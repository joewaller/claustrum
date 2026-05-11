#!/usr/bin/env python3
"""
bootstrap_taxonomy.py — one-time topic-taxonomy seeding for claustrum-cloud.

Pulls signal from your project's recent activity, clusters into named topic
buckets via a single LLM pass, and writes seed_topics.json. The output feeds
migration 0002_seed_topics.sql which seeds the `topics` table.

Inputs (combine as many as apply to your team):
  --github-org ORG        (required) Last 90d merged PRs across this org.
  --github-repo OWNER/REPO Repeatable. Narrower scope alternative.
  --include-work-history URL    Optional. HTTP endpoint returning recent task
                                 records as JSON: [{request, response_sent, ...}].
  --include-kg-domain DOMAIN    Optional, repeatable. Reads KG entries for
                                 named domains. URL+token via env:
                                   CLAUSTRUM_KG_URL, CLAUSTRUM_KG_TOKEN.

LLM:
  --llm gemini|claude|local    Which model performs the clustering pass.
                                Provider auth via env (GEMINI_API_KEY,
                                ANTHROPIC_API_KEY, or local endpoint URL).

Output:
  --out PATH    Default: seed_topics.json in cwd.

Usage examples:
  bootstrap_taxonomy.py --github-org joewaller --llm claude
  bootstrap_taxonomy.py --github-org finderau --include-work-history \
      https://gateway.finder.com/work-history/recent \
      --include-kg-domain engineering --include-kg-domain gateway \
      --llm gemini

This script is intentionally stdlib-only beyond `gh` CLI. Run it locally;
the output JSON is human-reviewable and source-controllable.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Source collectors — each returns a list of {kind, title, body} dicts
# ---------------------------------------------------------------------------

def collect_github_prs(org: str | None, repos: list[str]) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    queries: list[str] = []
    if org:
        queries.append(f"is:merged org:{org} merged:>{cutoff}")
    for repo in repos:
        queries.append(f"is:merged repo:{repo} merged:>{cutoff}")

    items: list[dict] = []
    for q in queries:
        try:
            result = subprocess.run(
                ["gh", "pr", "list", "--search", q, "--json",
                 "title,body,repository,number,mergedAt", "--limit", "1000"],
                capture_output=True, text=True, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"warning: gh query failed for '{q}': {e}", file=sys.stderr)
            continue

        for pr in json.loads(result.stdout or "[]"):
            items.append({
                "kind": "pr",
                "title": pr.get("title", ""),
                "body": (pr.get("body") or "")[:1500],
            })
    return items


def collect_work_history(url: str) -> list[dict]:
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            payload = json.loads(r.read())
    except Exception as e:
        print(f"warning: work-history fetch failed: {e}", file=sys.stderr)
        return []
    items = []
    for row in payload:
        items.append({
            "kind": "work-history",
            "title": (row.get("request") or "")[:200],
            "body": (row.get("response_sent") or "")[:1500],
        })
    return items


def collect_kg(domains: list[str]) -> list[dict]:
    base = os.environ.get("CLAUSTRUM_KG_URL")
    token = os.environ.get("CLAUSTRUM_KG_TOKEN")
    if not base:
        if domains:
            print("warning: CLAUSTRUM_KG_URL not set; skipping KG domains",
                  file=sys.stderr)
        return []

    items: list[dict] = []
    for domain in domains:
        url = f"{base.rstrip('/')}/query?domain={domain}"
        req = urllib.request.Request(url)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = json.loads(r.read())
        except Exception as e:
            print(f"warning: KG fetch failed for {domain}: {e}", file=sys.stderr)
            continue
        for entity in payload.get("entities", []):
            items.append({
                "kind": f"kg/{domain}",
                "title": entity.get("name", ""),
                "body": "\n".join(entity.get("observations", []))[:1500],
            })
    return items


# ---------------------------------------------------------------------------
# LLM clustering pass
# ---------------------------------------------------------------------------

CLUSTERING_PROMPT = """Given the artefacts below (PR titles+bodies, completed
tasks, KG entries), cluster them into 30 to 50 named topics suitable for
tagging future agent sessions.

Each topic gets a `name` (kebab-case, max 30 chars) and `description` (1 to 2
sentences). Topics should be specific enough that two sessions tagged the
same topic are likely working on the same thing, broad enough that new
sessions can usually find a fit.

Examples of good topic granularity:
  gateway-deploy        (deployment workflow for the MCP gateway)
  wally-routing         (Wally bot's request-routing layer)
  data-epv-tracker      (the EPV weekly tracker pipeline)

Output a JSON array. No prose.

Artefacts:
"""


def cluster_with_llm(items: list[dict], provider: str) -> list[dict]:
    artefact_lines = []
    for it in items:
        artefact_lines.append(f"[{it['kind']}] {it['title']}\n{it['body']}\n---")
    prompt = CLUSTERING_PROMPT + "\n".join(artefact_lines)

    if provider == "gemini":
        return _call_gemini(prompt)
    if provider == "claude":
        return _call_claude(prompt)
    if provider == "local":
        return _call_local(prompt)
    raise SystemExit(f"unknown llm provider: {provider}")


def _call_gemini(prompt: str) -> list[dict]:
    raise NotImplementedError(
        "Gemini integration is intentionally not in the scaffold PR. "
        "Wire it via google-genai SDK in a follow-up PR. "
        "Expected return shape: [{name: str, description: str}]"
    )


def _call_claude(prompt: str) -> list[dict]:
    raise NotImplementedError(
        "Claude integration is intentionally not in the scaffold PR. "
        "Wire it via anthropic SDK in a follow-up PR. "
        "Expected return shape: [{name: str, description: str}]"
    )


def _call_local(prompt: str) -> list[dict]:
    raise NotImplementedError(
        "Local-model integration is intentionally not in the scaffold PR. "
        "Wire it via OpenAI-compatible HTTP to a local endpoint "
        "(Ollama, LM Studio) in a follow-up PR."
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_seed_json(topics: list[dict], out_path: str) -> None:
    with open(out_path, "w") as f:
        json.dump(topics, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"wrote {len(topics)} topics to {out_path}")


def write_seed_sql(topics: list[dict], out_path: str) -> None:
    """Optional companion: emit 0002_seed_topics.sql alongside the JSON."""
    lines = [
        "-- AUTOGENERATED by bootstrap_taxonomy.py — do not hand-edit.",
        "-- Re-run bootstrap_taxonomy.py to refresh.",
        "",
    ]
    for t in topics:
        name = t["name"].replace("'", "''")
        desc = t["description"].replace("'", "''")
        lines.append(
            f"INSERT INTO topics (name, description, source) VALUES "
            f"('{name}', '{desc}', 'bootstrap') ON CONFLICT (name) DO NOTHING;"
        )
    lines.append("")
    lines.append("INSERT INTO _schema_migrations (version) VALUES "
                 "('0002_seed_topics') ON CONFLICT DO NOTHING;")
    with open(out_path, "w") as f:
        f.write("\n".join(lines))
    print(f"wrote SQL to {out_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--github-org", default=None,
                   help="GitHub org for last-90d merged PR query")
    p.add_argument("--github-repo", action="append", default=[],
                   help="Repeatable; narrower OWNER/REPO scope")
    p.add_argument("--include-work-history", default=None,
                   help="HTTP URL returning recent work-history JSON")
    p.add_argument("--include-kg-domain", action="append", default=[],
                   help="Repeatable; KG domain name (requires CLAUSTRUM_KG_URL)")
    p.add_argument("--llm", choices=["gemini", "claude", "local"], required=True)
    p.add_argument("--out", default="seed_topics.json")
    p.add_argument("--out-sql", default=None,
                   help="Optional path to also write SQL companion")
    args = p.parse_args()

    if not args.github_org and not args.github_repo:
        p.error("Provide --github-org or --github-repo (at least one)")

    items: list[dict] = []
    items.extend(collect_github_prs(args.github_org, args.github_repo))
    if args.include_work_history:
        items.extend(collect_work_history(args.include_work_history))
    items.extend(collect_kg(args.include_kg_domain))

    if not items:
        raise SystemExit("no source artefacts collected — check sources/auth")

    print(f"clustering {len(items)} artefacts with {args.llm}...")
    topics = cluster_with_llm(items, args.llm)
    write_seed_json(topics, args.out)
    if args.out_sql:
        write_seed_sql(topics, args.out_sql)


if __name__ == "__main__":
    main()
