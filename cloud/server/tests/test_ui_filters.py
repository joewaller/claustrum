"""Unit tests for the UI filter helper.

The ILIKE query behaviour, the archive pagination tiebreaker, and the new
session filter are SQL-level and exercised end-to-end against a real Postgres
(test-local.sh layer 2 / the staging deploy) — the same way the rest of the
server's SQL is gated. What lives here is the pure piece that decides *what*
gets passed to those queries: `_like`, which turns a free-text filter term into
a safe substring pattern.
"""

from app.routes.ui import _like


def test_none_stays_none():
    # A missing filter must stay None so the `(%(x)s::text IS NULL OR ...)`
    # guard skips the clause entirely.
    assert _like(None) is None


def test_wraps_term_in_substring_wildcards():
    # `joe.waller` (the short email shown in the User column) must match the
    # stored `joe.waller@finder.com` — the whole point of the switch to ILIKE.
    assert _like("joe.waller") == "%joe.waller%"


def test_escapes_like_wildcards_so_they_match_literally():
    # A literal % / _ in user input must not become a wildcard (otherwise `%`
    # alone would match every row).
    assert _like("100%") == "%100\\%%"
    assert _like("a_b") == "%a\\_b%"


def test_escapes_backslash_first():
    # Backslash is the default ILIKE escape char; escape it before % / _ so the
    # escaping of those isn't itself re-escaped.
    assert _like("a\\b") == "%a\\\\b%"
