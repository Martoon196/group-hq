"""The nightly entitlement recompute (Render cron, 03:10 UTC) and its on-demand form.

    python -m app.jobs.recompute                       # live, unless CONNECTORS_DRY_RUN=1
    python -m app.jobs.recompute --dry-run             # record and print the calls, send nothing
    python -m app.jobs.recompute --dry-run --today 2026-11-07    # see what the rule would do on another day

--today is only accepted with --dry-run: a live run as of another day would re-grant lapsed people
(back-dated) or revoke everyone (forward-dated).

Prints one line per call made or planned and a summary line, and exits 1 when any call failed so the
cron run shows as failed. The schema is Alembic's; this job never migrates.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from ..db import SessionLocal
from ..services.entitlements import RunSummary, recompute_all


def _parse_today(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise SystemExit(f"--today must be a date like 2026-11-07, not {raw!r}")


def _report(summary: RunSummary, out=sys.stdout) -> None:
    for decision in summary.calls:
        until = decision.ends_at.date().isoformat() if decision.ends_at else "-"
        print(f"  {decision.outcome:8} {decision.action:8} {decision.person_key:40} {decision.label:36} until {until}"
              f"{'  ' + decision.detail if decision.detail and decision.detail not in ('sent', 'planned') else ''}",
              file=out)
    print(summary.line(), file=out)


def main(argv: list[str] | None = None, *, out=sys.stdout) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.jobs.recompute", description="Recompute every mentee's entitlements.")
    parser.add_argument("--dry-run", action="store_true", help="record the calls and print them, send nothing")
    parser.add_argument("--today", help="run the rule as of this date (YYYY-MM-DD); defaults to today, UTC")
    args = parser.parse_args(argv)
    if args.today and not args.dry_run:
        parser.error("--today is only allowed with --dry-run: a live run is always as of today")
    today = _parse_today(args.today)
    db = SessionLocal()
    try:
        summary = recompute_all(db, today=today, dry_run=True if args.dry_run else None)
    finally:
        db.close()
    _report(summary, out)
    return 1 if summary.failed or summary.note else 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
