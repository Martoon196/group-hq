"""The growth pulls (Render crons group-hq-growth-hourly at :07 and group-hq-growth-daily at 06:10 UTC).

    python -m app.jobs.growth_pull --tier hourly                 # Stripe, the four product ledgers, GA4 realtime
    python -m app.jobs.growth_pull --tier daily                  # GA4, Search Console, Brevo, Zernio, Neon (and Clerk)
    python -m app.jobs.growth_pull --tier all --dry-run          # every source's plan, every header redacted
    python -m app.jobs.growth_pull --source neon --since 2026-10-01   # a backfill of one source (daily tier only)

Every source that is On (Growth > Sources) and runs in the tier is pulled for its own re-pull window (or from
--since) and written in one transaction of its own; a source switched Off is skipped silently, and a source not built
yet is named only when asked for by --source. One line per source (ok ga4 412 rows 2.1s / failed brevo
unauthorized (HTTP 401): Key not found / skipped linkedin not_built), then a summary line, and exit 1 when any source
that is On failed, or is On but not configured in this job's own environment (the crons copy their variables from
the web service one by one, in render.yaml: a missing fromService entry shows here first). Exit 1 too, before
anything else and with nothing done, when the database is not at this build's Alembic head: the crons deploy apart
from the web service, and a build that does not match the schema must not write to it.

Dry run (--dry-run, or CONNECTORS_DRY_RUN=1 on the server): nothing is sent to a platform and nothing is written to the
metrics tables; each source's plan is printed and kept on its metric_runs row. A source that is On but not configured
here is still planned in a dry run (plans need no credentials) and only named as a warning. A run in which at least one
source ran is audited once, as growth.pull by job:growth, with its tier and, per source, whether this job's
environment held the source's variables (the Sources screen reads the latest real run of each tier for its Cron
column); a run with every source Off writes nothing at all. One source that raises past run_source's own guards is
rolled back, printed as failed and counted, and the job goes on to the next.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from .. import migrate
from ..db import SessionLocal
from ..models import SOURCES
from ..services import settings
from ..services.audit import audit
from ..services.growth.collectors import COLLECTORS, SOURCE_INFO, runs_in, scrub, source_dry_run, source_mode
from ..services.growth.runner import JOB_ACTOR, SourceRun, run_source


def schema_problem() -> str:
    """Why this build must not run against the database, or "" when the database is at this build's head."""
    current, head = migrate.current_revision(), migrate.head_revision()
    if current == head:
        return ""
    return (f"the database is at revision {current or 'none'} but this build expects {head}: deploy the growth crons "
            "from the same commit as the web service (whose pre-deploy step migrates the database); nothing was done")


def _since(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise SystemExit(f"--since must be a date like 2026-10-01, not {raw!r}")


def main(argv: list[str] | None = None, *, out=sys.stdout) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.jobs.growth_pull", description="Pull the growth metrics.")
    parser.add_argument("--tier", choices=("hourly", "daily", "all"), default="all")
    parser.add_argument("--source", action="append", default=[], help="only this source (repeatable)")
    parser.add_argument("--since", help="backfill from this date (YYYY-MM-DD); daily tier only")
    parser.add_argument("--dry-run", action="store_true", help="print and record the plans; send nothing, write nothing")
    args = parser.parse_args(argv)
    unknown = [s for s in args.source if s not in SOURCES]
    if unknown:
        parser.error(f"unknown source: {', '.join(unknown)} (one of {', '.join(SOURCES)})")
    if args.since and args.tier == "hourly":
        parser.error("--since is for a backfill on the daily tier: the hourly tier always pulls its own window")
    since = _since(args.since)
    problem = schema_problem()
    if problem:
        print(f"growth_pull refused: {problem}", file=out)
        return 1

    dry = source_dry_run(args.dry_run)
    db = SessionLocal()
    outcomes: list[SourceRun] = []
    failed = not_configured = 0
    try:
        switches = settings.growth_source_switches(db)
        wanted = args.source or list(SOURCES)
        for source in wanted:
            named = source in args.source
            if source not in COLLECTORS:
                if named:
                    outcome = SourceRun(source=source, tier=SOURCE_INFO[source]["tier"], state="skipped", reason="not_built")
                    print(f"  {outcome.line()}", file=out)
                continue
            if not runs_in(source, args.tier):
                if named:
                    print(f"  skipped {source} not in the {args.tier} tier", file=out)
                continue
            mode = source_mode(source, switches=switches)
            if mode["switch"] == "off":
                if named:
                    print(f"  skipped {source} off", file=out)
                continue
            tier = args.tier if args.tier in ("hourly", "daily") else SOURCE_INFO[source]["tier"]
            if not mode["configured"]:
                missing = ", ".join(mode["missing"]) or "its settings"
                if dry:
                    print(f"  warning: {source} is On but not configured in this job's environment ({missing}); planning it anyway",
                          file=out)
                else:
                    not_configured += 1
                    print(f"  warning: {source} is On but this job has no {missing}: copy it to this cron (render.yaml "
                          f"fromService) or switch {source} Off on Growth > Sources", file=out)
            try:
                outcome = run_source(db, source, tier=tier, since=since if tier == "daily" else None, dry_run=args.dry_run,
                                     actor=JOB_ACTOR)
            except Exception as exc:   # noqa: BLE001  (one source must never stop the others)
                db.rollback()
                outcome = SourceRun(source=source, tier=tier, state="failed", reason="error",
                                    error=scrub(f"{type(exc).__name__}: the run could not be completed or recorded")[:500])
            outcomes.append(outcome)
            print(f"  {outcome.line()}", file=out)
            for note in outcome.notes[:10]:
                print(f"    note: {note}", file=out)
            if outcome.state == "dry_run":
                for plan in outcome.plans:
                    print("    plan: " + json.dumps(plan, ensure_ascii=False, default=str), file=out)
            if outcome.state == "failed" and outcome.reason != "not_configured":
                failed += 1
        if outcomes:
            summary = {"tier": args.tier, "dry_run": dry, "since": since.isoformat() if since else None,
                       "sources": {o.source: {"state": o.state, "rows": o.rows, "error": o.error[:200] or None}
                                   for o in outcomes},
                       "configured": {o.source: o.reason != "not_configured" for o in outcomes}}
            try:
                audit(db, JOB_ACTOR, "growth.pull", "job:growth", after=summary)
                db.commit()
            except Exception as exc:   # noqa: BLE001  (the runs are already recorded; say so and fail the run)
                db.rollback()
                failed += 1
                print(f"  failed to write the growth.pull audit row: {type(exc).__name__}", file=out)
    finally:
        db.close()
    ok = sum(1 for o in outcomes if o.state in ("ok", "dry_run"))
    rows = sum(o.rows for o in outcomes)
    print(f"growth_pull {args.tier}{' (dry run)' if dry else ''}: {len(outcomes)} source{'s' if len(outcomes) != 1 else ''} run, "
          f"{ok} ok, {failed} failed, {not_configured} not configured, {rows} rows written", file=out)
    return 1 if failed or not_configured else 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
