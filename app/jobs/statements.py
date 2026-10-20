"""The nightly commission-statement job (plan, Phase 4; docs/commission-rules.md section 4).

    python -m app.jobs.statements                       # on the 10th: statements for the previous month; any other day, nothing
    python -m app.jobs.statements --period 2026-10      # statements for that month, whatever the date
    python -m app.jobs.statements --dry-run             # report what would be generated and write nothing
    python -m app.jobs.statements --today 2026-11-10 --dry-run   # see what the rule would do on another day

--today is only accepted with --dry-run, as in app.jobs.recompute: a live run is always as of today.

Running it twice for the same period is safe: an unpaid statement is recomputed and gains only entries that
became payable since; a paid one is never touched (app.services.commission.generate_statements). Prints one
line per statement and a summary line; exits 1 when the period is refused so the cron run shows as failed.
The schema is Alembic's; this job never migrates.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from ..db import SessionLocal
from ..services import commission
from ..services.commission import CommissionError, pounds


def _parse_today(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise SystemExit(f"--today must be a date like 2026-11-10, not {raw!r}")


def _report(run: commission.StatementRun, *, dry_run: bool, out=sys.stdout) -> None:
    verb = "would generate" if dry_run else "generated"
    for statement in run.generated:
        print(f"  {verb:16} statement #{statement.id:<5} seller #{statement.seller_id:<5} {pounds(statement.total_pence):>14}", file=out)
    for statement in run.regenerated:
        print(f"  {'would recompute' if dry_run else 'recomputed':16} statement #{statement.id:<5} seller #{statement.seller_id:<5} {pounds(statement.total_pence):>14}", file=out)
    for seller, count, pence in run.held:
        print(f"  {'held':16} seller {seller.code:<12} {count} entr{'ies' if count != 1 else 'y'} {pounds(pence):>14}  no self-billing agreement", file=out)
    for statement in run.paid_already:
        print(f"  {'already paid':16} statement #{statement.id:<5} seller #{statement.seller_id:<5} {pounds(statement.total_pence):>14}", file=out)
    print(("DRY RUN: " if dry_run else "") + run.line(), file=out)


def main(argv: list[str] | None = None, *, out=sys.stdout) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.jobs.statements", description="Generate the sellers' commission statements.")
    parser.add_argument("--period", help="the month to generate, YYYY-MM; defaults to the previous month, and only on the 10th")
    parser.add_argument("--dry-run", action="store_true", help="report what would be generated, write nothing")
    parser.add_argument("--today", help="run the rule as of this date (YYYY-MM-DD); defaults to today, UTC")
    args = parser.parse_args(argv)
    if args.today and not args.dry_run:
        parser.error("--today is only allowed with --dry-run: a live run is always as of today")
    today = _parse_today(args.today) or commission.utcnow().date()
    if args.period:
        try:
            period = commission.parse_period(args.period)
        except CommissionError as e:
            print(str(e), file=out)
            return 1
    elif commission.statement_due(today):
        period = commission.previous_month(today)
    else:
        print(f"{today:%d %B %Y} is not the {commission.STATEMENT_DAY}th; statements are generated on the {commission.STATEMENT_DAY}th "
              f"for the previous month. Pass --period YYYY-MM to generate one today.", file=out)
        return 0
    if period[0] > today:
        print(f"{commission.period_label(period[0])} has not started; a statement is for a month that has ended.", file=out)
        return 1
    db = SessionLocal()
    try:
        try:
            run = commission.generate_statements(db, period, actor=commission.JOB_ACTOR)
        except CommissionError as e:
            db.rollback()
            print(str(e), file=out)
            return 1
        if args.dry_run:
            db.flush()
            _report(run, dry_run=True, out=out)
            db.rollback()
        else:
            db.commit()
            _report(run, dry_run=False, out=out)
    finally:
        db.close()
    return 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
