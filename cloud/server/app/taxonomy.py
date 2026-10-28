"""Name normalisation, relatedness, alias resolution and merge proposals for the
domain/topic taxonomy. Pure functions (no DB) so every policy here is
unit-testable; the routes do the SQL around them.

The client (`claustrum`, zero-dependency) carries a copy of `norm_token` /
`norm_tokens` for candidate ranking — tests/test_taxonomy.py pins the two
implementations to the same output so they cannot drift.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Taxonomy names are compared en-US, singular. Explicit pairs cover the en-AU
# spellings the suffix rules below can't (or shouldn't) generalise.
_SPELLING = {
    "artefact": "artifact",
    "colour": "color",
    "favourite": "favorite",
    "behaviour": "behavior",
    "centre": "center",
    "metre": "meter",
    "theatre": "theater",
    "fibre": "fiber",
    "licence": "license",
    "defence": "defense",
    "offence": "offense",
    "programme": "program",
    "catalogue": "catalog",
    "dialogue": "dialog",
    "analogue": "analog",
    "cheque": "check",
    "enrol": "enroll",
    "enrolment": "enrollment",
    "travelling": "traveling",
    "cancelled": "canceled",
    "modelling": "modeling",
    "labelling": "labeling",
}

# Short-hands the fleet uses interchangeably with the full name. Kept tiny and
# unambiguous — 'wp-mcp' and 'wordpress-mcp' are the same work.
_ABBREVIATIONS = {
    "wp": "wordpress",
    "bq": "bigquery",
    "gh": "github",
    "k8s": "kubernetes",
}

# Words the suffix rules would mangle into a collision with a different word
# ('stats' and 'state' both -> 'stat', 'canvas' -> 'canva'). Mapped directly and
# exempt from every rule below.
_IRREGULAR = {
    "stats": "statistic",
    "statistics": "statistic",
    "canvas": "canvas",
    "canvases": "canvas",
    "alias": "alias",
    "aliases": "alias",
    "atlas": "atlas",
    "news": "news",
    "series": "series",
    "plane": "plane",
    "planes": "plane",
}

_ISE = re.compile(r"^(\w{3,})is(e|ed|er|ing|ation)$")
_YSE = re.compile(r"^(\w{3,})ys(e|ed|er|ing)$")
_OUR = re.compile(r"^(\w{3,})our$")


def _singular(t: str) -> str:
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")):
        return t[:-1]
    return t


def norm_token(token: str) -> str:
    """Canonical comparison form of one name token: lowercase, singular, en-US,
    abbreviations expanded, trailing 'e' dropped (so 'cache'/'caches' and
    'page'/'pages' meet). Only ever used for comparison — never displayed."""
    t = (token or "").lower()
    if t in _IRREGULAR:
        return _IRREGULAR[t]
    t = _singular(t)
    if t in _ABBREVIATIONS:
        return norm_token(_ABBREVIATIONS[t])
    t = _SPELLING.get(t, t)
    t = _ISE.sub(r"\1iz\2", t)
    t = _YSE.sub(r"\1yz\2", t)
    t = _OUR.sub(r"\1or", t)
    if len(t) > 4 and t.endswith("e"):
        t = t[:-1]
    return t


@lru_cache(maxsize=16384)
def en_au_score(name: str | None) -> int:
    """How many of a name's tokens use the en-AU spelling of an en-AU/en-US pair
    the normaliser knows (artefact, colour, -ise, -yse, licence...). Joe's rule:
    between spelling variants used equally, the Australian form is canonical.
    Only meaningful when comparing variants of one name — tokens spelt the same
    in both (promise, detour) count on both sides and cancel out."""
    score = 0
    for raw in re.split(r"[^a-z0-9]+", (name or "").lower()):
        if not raw or raw in _IRREGULAR:
            continue
        t = _singular(raw)
        if t in _SPELLING or _ISE.match(t) or _YSE.match(t) or _OUR.match(t):
            score += 1
    return score


@lru_cache(maxsize=16384)
def norm_tokens(name: str | None) -> frozenset:
    return frozenset(
        norm_token(t) for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t
    )


# Tokens that describe the KIND of work rather than its subject. Removing them
# leaves a name's "core" — 'artifacts-app' and 'artifacts-site' share the core
# {artifact}. 'mcp' is deliberately NOT generic: wordpress-mcp is distinct from
# wordpress (decided 2026-06-30, see migration 0009).
GENERIC_TOKENS = frozenset(norm_token(t) for t in (
    "app", "site", "ui", "ux", "api", "setup", "fix", "work", "team", "tool",
    "dev", "test", "testing", "automation", "integration", "improvement",
    "update", "refresh", "build", "feature", "support", "project",
    "task", "page", "finder", "com", "au", "and", "the", "for", "of", "to",
    "with", "v2", "core", "system", "service", "management", "flow",
))


@lru_cache(maxsize=16384)
def core_tokens(name: str | None) -> frozenset:
    return frozenset(t for t in norm_tokens(name) if t not in GENERIC_TOKENS)


@lru_cache(maxsize=16384)
def stem(name: str | None) -> str | None:
    """The first subject-bearing token of a name — the 'family' it belongs to
    ('artefacts-reviews' -> 'artifact'). None when the name is all generic."""
    for t in re.split(r"[^a-z0-9]+", (name or "").lower()):
        if not t:
            continue
        n = norm_token(t)
        if n not in GENERIC_TOKENS:
            return n
    return None


def jaccard(a, b) -> float:
    a, b = set(a), set(b)
    union = len(a | b)
    return len(a & b) / union if union else 0.0


# Token-Jaccard at/above which a proposed name duplicates an existing one.
# Deliberately HIGH: the judge already made the semantic pick-vs-propose call;
# this only catches surface variants.
DUP_JACCARD = 0.7
# Looser bar for "related" (overlap tier t3b, merge candidates).
RELATED_JACCARD = 0.5


def is_near_duplicate(a: str, b: str) -> bool:
    """Same name up to case/punctuation/order/spelling/plurals, high token
    overlap, or one name is the other's bare subject plus generic kind-of-work
    tokens ('artifacts' ~ 'artefacts-app'). Two qualified variants of one
    subject ('checkout-api' / 'checkout-ui') are NOT duplicates — that may be
    distinct work, so it is left to reviewed merges."""
    na, nb = norm_tokens(a), norm_tokens(b)
    if not na or not nb:
        return False
    if na == nb or jaccard(na, nb) >= DUP_JACCARD:
        return True
    ca, cb = core_tokens(a), core_tokens(b)
    return bool(ca) and ca == cb and (ca == na or cb == nb)


def near_duplicate(name: str, existing: list[str]) -> str | None:
    """The closest existing near-duplicate: highest normalised token overlap,
    then the en-AU spelling, then the shortest name. None when nothing matches.
    No usage counts here — the propose path only has names."""
    nt = norm_tokens(name)
    matches = [e for e in existing if is_near_duplicate(name, e)]
    if not matches:
        return None
    return min(matches, key=lambda e: (-jaccard(nt, norm_tokens(e)), -en_au_score(e),
                                       len(e), e))


# The bootstrap domains (0006 seed) are broad buckets — sharing one says little
# about overlap, so they never count as "related" on their own.
BROAD_DOMAINS = frozenset({
    "actions", "company", "data", "engineering", "gateway", "general", "people",
    "projects", "security", "strategy", "tooling", "wally",
})


def topic_relation(
    my_topic: str | None,
    my_domain: str | None,
    their_topic: str | None,
    their_domain: str | None,
) -> str | None:
    """Why two (already alias-resolved) topics are related, or None. The reason
    string is surfaced to the user, strongest first:
      topic~<name>  near-duplicate / high-overlap name
      stem:<token>  same subject family ('artifacts-app' / 'artefacts-reviews')
      domain:<name> same specific (non-broad) domain
    Identical topics are t3, not related — callers handle that first."""
    if not their_topic or my_topic == their_topic:
        return None
    if my_topic:
        if is_near_duplicate(my_topic, their_topic) or (
            jaccard(norm_tokens(my_topic), norm_tokens(their_topic)) >= RELATED_JACCARD
        ):
            return f"topic~{their_topic}"
        s = stem(my_topic)
        if s and s == stem(their_topic):
            return f"stem:{s}"
    if my_domain and my_domain == their_domain and my_domain not in BROAD_DOMAINS:
        return f"domain:{my_domain}"
    return None


# ---------------------------------------------------------------------------
# Aliases — approved merges take effect at read time, history is never rewritten
# ---------------------------------------------------------------------------

def resolve(name: str | None, aliases: dict | None) -> str | None:
    """Follow alias -> canonical hops. Cycle-safe (a bad approval can't hang a
    request) and bounded."""
    if not name or not aliases:
        return name
    seen = {name}
    cur = name
    for _ in range(16):
        nxt = aliases.get(cur)
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        cur = nxt
    return cur


def build_alias_map(parent_rows, alias_rows) -> dict:
    """Merge the two canonicalisation sources into one {alias: canonical} map:
    the legacy `parent` column (variant -> parent) and approved
    `taxonomy_aliases` rows. An approved alias wins over a parent."""
    out = {a: p for a, p in parent_rows if a and p and a != p}
    out.update({a: c for a, c in alias_rows if a and c and a != c})
    return out


def alias_group(canonical: str | None, aliases: dict) -> list[str]:
    """Every name that resolves to `canonical`, including itself."""
    if not canonical:
        return []
    return sorted({canonical} | {a for a in aliases if resolve(a, aliases) == canonical})


# ---------------------------------------------------------------------------
# Merge proposals (/jobs/topic-merge) — proposals only, never auto-applied
# ---------------------------------------------------------------------------

ADOPTER_JACCARD = 0.5  # adopter-set overlap that corroborates a related name
# Without a near-duplicate name, adopter overlap only counts when BOTH names
# have at least this many distinct adopters: with 1-2 people a Jaccard of 1.0
# is trivial (bing-ads-us ~ google-ads-us, team-ai-adoption ~ artifacts-*).
MIN_ADOPTERS_FOR_ADOPTER_MATCH = 3


def _canonical_rank(entry: dict) -> tuple:
    """Lower sorts first (= better canonical): names in the taxonomy over
    free-text ones (a canonical must be a real topic/domain), then most
    adopters, most sessions; on a usage tie the en-AU spelling (Joe's rule:
    artefacts over artifacts, colour over color), then bootstrap over emergent,
    then the shorter/alphabetical name for stability."""
    return (
        0 if entry.get("source") else 1,
        -len(entry.get("adopters") or ()),
        -(entry.get("sessions") or 0),
        -en_au_score(entry["name"]),
        0 if entry.get("source") == "bootstrap" else 1,
        len(entry["name"]),
        entry["name"],
    )


def merge_pair_reasons(a: dict, b: dict) -> tuple[float, list[str]] | None:
    """Score one pair of taxonomy entries ({name, adopters:set, sessions,
    source}). Returns (score, reasons) when the pair should be proposed for a
    merge, else None.

      name     — near-duplicate after normalisation: always proposed
      related  — related name (stem / overlap) AND adopter Jaccard >= 0.5
      adopters — adopter Jaccard > 0.5, names unrelated
    Both adopter-based reasons need MIN_ADOPTERS_FOR_ADOPTER_MATCH distinct
    adopters on EACH side: small sets overlap perfectly by construction, so
    their overlap corroborates nothing."""
    na, nb = a["name"], b["name"]
    ua, ub = set(a.get("adopters") or ()), set(b.get("adopters") or ())
    adopter_j = jaccard(ua, ub)
    reasons: list[str] = []
    name_dup = is_near_duplicate(na, nb)
    name_j = jaccard(norm_tokens(na), norm_tokens(nb))
    same_stem = bool(stem(na)) and stem(na) == stem(nb)
    enough_adopters = min(len(ua), len(ub)) >= MIN_ADOPTERS_FOR_ADOPTER_MATCH
    if name_dup:
        reasons.append("name")
    elif not enough_adopters:
        return None
    elif (same_stem or name_j >= RELATED_JACCARD) and adopter_j >= ADOPTER_JACCARD:
        reasons.append("related")
    elif adopter_j > ADOPTER_JACCARD:
        reasons.append("adopters")
    else:
        return None
    if same_stem:
        reasons.append(f"stem:{stem(na)}")
    if adopter_j:
        reasons.append(f"adopters_j={adopter_j:.2f}")
    score = round(
        (1.0 if name_dup else max(name_j, 0.5 if same_stem else 0.0)) + adopter_j, 3
    )
    return score, reasons


def _candidate_pairs(entries: list[dict], folded: set) -> list[tuple]:
    """Pairs that could possibly qualify — sharing a normalised name token or an
    adopter — via inverted indexes, instead of all n^2 pairs of a ~2k-name
    taxonomy."""
    index: dict = {}
    for e in entries:
        if e["name"] in folded:
            continue
        keys = [("t", t) for t in norm_tokens(e["name"])]
        keys += [("u", u) for u in (e.get("adopters") or ())]
        for k in keys:
            index.setdefault(k, []).append(e["name"])
    pairs = set()
    for names in index.values():
        names = sorted(set(names))
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                pairs.add((a, b))
    return sorted(pairs)


def propose_merges(
    entries: list[dict], *, folded: set | None = None, rejected: set | None = None
) -> list[dict]:
    """Emit {alias, canonical, score, reasons} merge proposals, strongest first.

    Only near-duplicate NAME edges cluster transitively (union-find): spelling
    and suffix variants of one subject collapse onto the best-ranked member.
    Weaker 'related' / 'adopters' edges never chain — each proposes merging the
    lower-ranked cluster canonical into the higher-ranked one, one hop, keeping
    the best edge per alias. Ranking is a total order, so proposals can't form a
    cycle. `folded` names (already an approved alias or legacy variant) are left
    out entirely; `rejected` names are never proposed as an alias again but may
    still be another name's canonical."""
    folded = folded or set()
    rejected = rejected or set()
    by_name = {e["name"]: e for e in entries}
    parent = {n: n for n in by_name}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    name_edges: dict[tuple, tuple] = {}
    weak_edges: dict[tuple, tuple] = {}
    for a, b in _candidate_pairs(entries, folded):
        hit = merge_pair_reasons(by_name[a], by_name[b])
        if hit is None:
            continue
        if hit[1][0] == "name":
            name_edges[(a, b)] = hit
            parent[find(a)] = find(b)
        else:
            weak_edges[(a, b)] = hit

    best_name_edge: dict[str, tuple] = {}
    for pair, hit in name_edges.items():
        for n in pair:
            if n not in best_name_edge or hit[0] > best_name_edge[n][0]:
                best_name_edge[n] = hit

    clusters: dict[str, list[str]] = {}
    for n in best_name_edge:
        clusters.setdefault(find(n), []).append(n)
    canonical_of: dict[str, str] = {}
    proposals: dict[str, dict] = {}
    for members in clusters.values():
        canonical = min((by_name[m] for m in members), key=_canonical_rank)["name"]
        if not by_name[canonical].get("source"):
            continue  # all free-text: no approvable canonical yet
        for m in members:
            canonical_of[m] = canonical
            if m == canonical or m in rejected:
                continue
            pair = tuple(sorted((m, canonical)))
            score, reasons = name_edges.get(pair) or best_name_edge[m]
            if pair not in name_edges:
                reasons = [*reasons, "via-cluster"]
            proposals[m] = {"alias": m, "canonical": canonical,
                            "score": score, "reasons": reasons}

    for (a, b), (score, reasons) in weak_edges.items():
        ca, cb = canonical_of.get(a, a), canonical_of.get(b, b)
        if ca == cb:
            continue
        hi, lo = sorted((ca, cb), key=lambda n: _canonical_rank(by_name[n]))
        if lo in rejected or not by_name[hi].get("source"):
            continue
        if lo in proposals and proposals[lo]["score"] >= score:
            continue
        proposals[lo] = {"alias": lo, "canonical": hi, "score": score, "reasons": reasons}

    out = list(proposals.values())
    out.sort(key=lambda r: (-r["score"], r["canonical"], r["alias"]))
    return out
