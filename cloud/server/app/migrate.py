"""Apply pending SQL migrations to Claustrum Cloud.

Designed to run as a Cloud Run Job alongside the API service, reusing the
same image, service account, and `CLAUSTRUM_DB_URL` secret. Triggered from
the deploy script via:

    gcloud run jobs execute <name> --region=<region> --wait

Idempotent: each migration is tracked in `_schema_migrations` and skipped
on re-run. Bootstraps that table itself so the first migration can be
applied against an empty database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def main() -> int:
    conninfo = os.environ["CLAUSTRUM_DB_URL"]
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print("no migrations found", flush=True)
        return 0

    with psycopg.connect(conninfo, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS _schema_migrations ("
                "  version text PRIMARY KEY,"
                "  applied_at timestamptz NOT NULL DEFAULT now())"
            )
            conn.commit()

            for path in files:
                version = path.stem
                cur.execute(
                    "SELECT 1 FROM _schema_migrations WHERE version = %s",
                    (version,),
                )
                if cur.fetchone() is not None:
                    print(f"  skip {version} (already applied)", flush=True)
                    continue

                print(f"  apply {version}", flush=True)
                cur.execute(path.read_text())
                # Migrations may insert their own row; ON CONFLICT keeps this safe.
                cur.execute(
                    "INSERT INTO _schema_migrations (version) VALUES (%s) "
                    "ON CONFLICT (version) DO NOTHING",
                    (version,),
                )
                conn.commit()

    print("migrations complete", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
