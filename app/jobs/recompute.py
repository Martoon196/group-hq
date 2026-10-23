"""The nightly entitlement recompute (Render cron, 03:10 UTC) and its on-demand form.

    python -m app.jobs.recompute                       # live, unless CONNECTORS_DRY_RUN=1
    python -m app.jobs.recompute --dry-run             # record and print the calls, send nothing
    python -m app.jobs.recompute --dry-run --today 2026-11-07    # see what the rule would do on another day

--today is only accepted with --dry-run: a live run as of another day would re-grant lapsed people
(back-dated) or revoke everyone (forward-dated).

Each product is live or in dry run as its switch on the Settings screen says (the app_settings table, read at
the start of the run and again before each person, so a switch to Dry run stops a run under way), with
CONNECTORS_DRY_RUN=1 as a floor and the CONNECTORS_DRY_RUN list as the default for a product never switched; a
held grant is left revoked.

Prints how each product was treated, one line per call made or planned and a summary line, and exits 1 so the
cron run shows as failed when any call failed, when a product that is not in dry run has no token or base URL in
this job's own environment (the cron copies its tokens from the web service one by one, in render.yaml), or when
the database is not at this build's Alembic head. That last check runs before anything else: the cron is
deployed apart from the web service, and a build that does not match the schema would ignore what the schema
holds, such as the holds and the product switches (0006). The schema is Alembic's; this job never migrates.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from .. import migrate
from ..db import SessionLocal
from ..services.entitlements import RunSummary, recompute_all


def _parse_today(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise SystemExit(f"--today must be a date like 2026-11-07, not {raw!r}")


def schema_problem() -> str:
    """Why this build must not run against the database, or "" when the database is at this build's head."""
    current, head = migrate.current_revision(), migrate.head_revision()
    if current == head:
        return ""
    return (f"the database is at revision {current or 'none'} but this build expects {head}: deploy the recompute cron "
            "from the same commit as the web service (whose pre-deploy step migrates the database); nothing was done")


def unconfigured_products(summary: RunSummary) -> list[str]:
    """Products this run would have sent to but could not: not in dry run, and without a token or base URL here."""
    return [product for product, state in summary.products.items() if state == "not_configured"]


def _report(summary: RunSummary, out=sys.stdout) -> None:
    if summary.products:
        print("  products: " + ", ".join(f"{product} {state.replace('_', ' ')}" for product, state in summary.products.items()),
              file=out)
    for product in unconfigured_products(summary):
        print(f"  warning: {product} is not in dry run, but this job has no token or base URL for it, so nothing is sent "
              f"to it: copy its token to the recompute cron (render.yaml fromService) or switch it to Dry run on Settings",
              file=out)
    if summary.stopped:
        print("  stopped: " + ", ".join(summary.stopped) + " switched to dry run during the run; nothing more was sent to it",
              file=out)
    for decision in summary.calls:
        until = decision.ends_at.date().isoformat() if decision.ends_at else "-"
        print(f"  {decision.outcome:8} {decision.action:8} {decision.person_key:40} {decision.label:36} until {until}"
              f"{'  ' + decision.detail if decision.detail and decision.detail not in ('sent', 'planned') else ''}",
              file=out)
    for decision in summary.decisions:
        if decision.kind == "lookup":       # a PP account lookup that failed before a Key Properties link was sent
            print(f"  note     {decision.person_key:40} pp {decision.detail}", file=out)
    print(summary.line(), file=out)


def main(argv: list[str] | None = None, *, out=sys.stdout) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.jobs.recompute", description="Recompute every mentee's entitlements.")
    parser.add_argument("--dry-run", action="store_true", help="record the calls and print them, send nothing")
    parser.add_argument("--today", help="run the rule as of this date (YYYY-MM-DD); defaults to today, UTC")
    args = parser.parse_args(argv)
    if args.today and not args.dry_run:
        parser.error("--today is only allowed with --dry-run: a live run is always as of today")
    today = _parse_today(args.today)
    problem = schema_problem()
    if problem:
        print(f"recompute refused: {problem}", file=out)
        return 1
    db = SessionLocal()
    try:
        summary = recompute_all(db, today=today, dry_run=True if args.dry_run else None)
    finally:
        db.close()
    _report(summary, out)
    return 1 if summary.failed or summary.note or unconfigured_products(summary) else 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
