#!/usr/bin/env python3
"""Per-tenant usage reporting for chargeback.

Backlog item 34.

**This is deliberately a script and not an endpoint, and it deliberately does
not import `app`.**

Chargeback is inherently a cross-tenant question, and this platform has exactly
one escape from tenant isolation — `system_scope()` — which `tests/test_tenancy.py`
pins to a single caller, the queue consumer, on the grounds that a deliberate
hole stays deliberate only while it stays small. Adding a reporting path inside
`app/` would either widen that hole or need a second one. So this reads the
database directly, as an operator tool, in the same category as `pg_dump`.

That has a consequence you must not skip: it needs a role that can see every
tenant's rows, which means a role that **bypasses row-level security**. Use a
dedicated read-only reporting role. Never the application's role — and note
that `Database.assert_row_level_security_applies()` already refuses to *start*
the platform on such a role, which is the same rule seen from the other side.

    CREATE ROLE k8sagent_report LOGIN PASSWORD '…' BYPASSRLS;
    GRANT CONNECT ON DATABASE k8sagent TO k8sagent_report;
    GRANT USAGE ON SCHEMA public TO k8sagent_report;
    GRANT SELECT ON investigations, investigation_reports TO k8sagent_report;

Usage:

    python scripts/tenant_usage.py --dsn "$REPORTING_DATABASE_URL" --days 30
    python scripts/tenant_usage.py --dsn … --days 30 --format csv > usage.csv
    python scripts/tenant_usage.py --dsn … --since 2026-07-01 --until 2026-08-01
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, datetime, timedelta

# One row per tenant. `result` is never selected — the payload averages 2.7 MB
# at the MAX_LIST_ITEMS ceiling, and a chargeback report that pulls it would
# move gigabytes to count rows. Same rule as `_JOB_SUMMARY_COLUMNS`.
USAGE_SQL = """
SELECT
    coalesce(tenant_id, 'default')                            AS tenant,
    count(*)                                                  AS investigations,
    count(*) FILTER (WHERE status = 'succeeded')              AS succeeded,
    count(*) FILTER (WHERE status = 'failed')                 AS failed,
    count(*) FILTER (WHERE status = 'cancelled')              AS cancelled,
    count(DISTINCT owner) FILTER (WHERE owner <> '')          AS distinct_users,
    count(DISTINCT request->>'cluster')                       AS distinct_clusters,
    coalesce(round(avg(
        EXTRACT(EPOCH FROM (finished_at - started_at))
    ) FILTER (WHERE finished_at IS NOT NULL AND started_at IS NOT NULL)::numeric, 3), 0)
                                                              AS avg_seconds,
    coalesce(round(sum(
        EXTRACT(EPOCH FROM (finished_at - started_at))
    ) FILTER (WHERE finished_at IS NOT NULL AND started_at IS NOT NULL)::numeric, 1), 0)
                                                              AS total_seconds
FROM investigations
WHERE created_at >= %(since)s AND created_at < %(until)s
GROUP BY 1
ORDER BY investigations DESC
"""

# Rendered artefacts are the storage side of the bill. Kept separate because
# retention prunes these on its own schedule while the history entry survives,
# so the two counts legitimately disagree.
STORAGE_SQL = """
SELECT
    coalesce(tenant_id, 'default')  AS tenant,
    count(*)                        AS stored_reports,
    coalesce(sum(length(content)), 0) AS stored_bytes
FROM investigation_reports
WHERE created_at >= %(since)s AND created_at < %(until)s
GROUP BY 1
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Per-tenant usage for chargeback.",
        epilog="Requires a role that bypasses row-level security. Never the application role.",
    )
    parser.add_argument("--dsn", required=True, help="Postgres DSN for the reporting role.")
    parser.add_argument("--days", type=int, default=30, help="Window ending now. Default 30.")
    parser.add_argument("--since", help="ISO date. Overrides --days.")
    parser.add_argument("--until", help="ISO date, exclusive. Defaults to now.")
    parser.add_argument(
        "--format", choices=("table", "csv", "json"), default="table", help="Default table."
    )
    return parser.parse_args(argv)


def window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    until = (
        datetime.fromisoformat(args.until).replace(tzinfo=UTC) if args.until else datetime.now(UTC)
    )
    if args.since:
        since = datetime.fromisoformat(args.since).replace(tzinfo=UTC)
    else:
        since = until - timedelta(days=args.days)
    if since >= until:
        raise SystemExit(f"empty window: {since.isoformat()} >= {until.isoformat()}")
    return since, until


def collect(dsn: str, since: datetime, until: datetime) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    params = {"since": since, "until": until}
    with (
        psycopg.connect(dsn, row_factory=dict_row) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(USAGE_SQL, params)
        rows = {row["tenant"]: dict(row) for row in cursor.fetchall()}
        cursor.execute(STORAGE_SQL, params)
        for row in cursor.fetchall():
            rows.setdefault(row["tenant"], {"tenant": row["tenant"]})
            rows[row["tenant"]].update(
                stored_reports=row["stored_reports"], stored_bytes=row["stored_bytes"]
            )

    for row in rows.values():
        row.setdefault("stored_reports", 0)
        row.setdefault("stored_bytes", 0)
        for key, default in (
            ("investigations", 0),
            ("succeeded", 0),
            ("failed", 0),
            ("cancelled", 0),
            ("distinct_users", 0),
            ("distinct_clusters", 0),
            ("avg_seconds", 0),
            ("total_seconds", 0),
        ):
            row.setdefault(key, default)
        # Decimal is not JSON-serialisable and reads badly in a table.
        for key in ("avg_seconds", "total_seconds"):
            row[key] = float(row[key])

    return sorted(rows.values(), key=lambda r: (-r["investigations"], r["tenant"]))


COLUMNS = (
    ("tenant", "tenant", 24),
    ("investigations", "runs", 8),
    ("succeeded", "ok", 7),
    ("failed", "fail", 6),
    ("cancelled", "canc", 6),
    ("distinct_users", "users", 7),
    ("distinct_clusters", "clusters", 9),
    ("avg_seconds", "avg s", 9),
    ("stored_reports", "reports", 9),
    ("stored_bytes", "bytes", 12),
)


def render_table(rows: list[dict], since: datetime, until: datetime) -> str:
    header = "  ".join(f"{title:<{width}}" for _, title, width in COLUMNS)
    lines = [
        f"Tenant usage  {since.date()} .. {until.date()}  (exclusive)",
        "",
        header,
        "-" * len(header),
    ]
    for row in rows:
        lines.append("  ".join(f"{_cell(row.get(key), key):<{width}}" for key, _, width in COLUMNS))
    if not rows:
        lines.append("(no investigations in this window)")
    else:
        lines.append("-" * len(header))
        lines.append(
            f"{'TOTAL':<24}  {sum(r['investigations'] for r in rows):<8}"
            f"  {sum(r['succeeded'] for r in rows):<7}"
            f"  {sum(r['failed'] for r in rows):<6}"
        )
    return "\n".join(lines)


def _cell(value, key: str) -> str:
    if value is None:
        return "-"
    if key == "stored_bytes":
        return _human_bytes(int(value))
    if key == "avg_seconds":
        return f"{float(value):.3f}"
    return str(value)


def _human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}TB"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    since, until = window(args)
    rows = collect(args.dsn, since, until)

    if args.format == "json":
        json.dump(
            {"since": since.isoformat(), "until": until.isoformat(), "tenants": rows},
            sys.stdout,
            indent=2,
            default=str,
        )
        sys.stdout.write("\n")
    elif args.format == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=[key for key, _, _ in COLUMNS])
        writer.writeheader()
        writer.writerows({key: row.get(key) for key, _, _ in COLUMNS} for row in rows)
    else:
        print(render_table(rows, since, until))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
