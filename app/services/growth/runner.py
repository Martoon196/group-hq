"""Run one collector and record what happened: the rows, a metric_runs row, the source's health row.

    outcome = run_source(db, "brevo", tier="daily", actor="job:growth")          # the cron, through app.jobs.growth_pull
    outcome = run_source(db, "brevo", tier="daily", actor=user)                   # Pull now on the Sources screen

One transaction per source: the rows, the metric_runs row and the metric_sources update commit together, and a
failure rolls the rows back but still records the run and the source's last error. In dry run (--dry-run, the tick
box, or CONNECTORS_DRY_RUN=1) nothing is written to the metrics tables and nothing reaches a platform: the plans, every
credential redacted, go into metric_runs.plan. Errors are stored as ConnectorError.summary() passed through scrub(),
so a platform that echoes a key back cannot put it in the database. If even that commit fails, the run is recorded
again with a short fallback error in a fresh transaction, and run_source still returns a failed SourceRun: one source
can never take the job down with it.

Health (metric_sources: status, last good run, last error) follows a source's own tier only (SOURCE_INFO). GA4's
hourly realtime read is a side line of a daily source: it keeps its metric_runs row and fails the hourly cron when it
fails, but it neither clears nor sets the daily pull's health, so the Visitors "as of" and the Sources row keep
showing the daily pull, which is where visitors come from.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from ...models import MetricRun, MetricSource, User, utcnow
from ..connectors.base import ConnectorError
from . import store
from .collectors import COLLECTORS, SOURCE_INFO, env_names, source_dry_run
from .collectors.base import scrub, today_london

JOB_ACTOR = "job:growth"
PLAN_LIMIT = 200          # plans kept on one metric_runs row


@dataclass
class SourceRun:
    source: str
    tier: str
    state: str = "ok"                    # ok, failed, dry_run, skipped
    rows: int = 0
    seconds: float = 0.0
    error: str = ""
    reason: str = ""
    notes: list = field(default_factory=list)
    plans: list = field(default_factory=list)
    since: date | None = None
    until: date | None = None
    run_id: int | None = None

    def line(self) -> str:
        if self.state == "ok":
            return f"ok {self.source} {self.rows} rows {self.seconds:.1f}s"
        if self.state == "dry_run":
            return f"dry-run {self.source} {len(self.plans)} request{'s' if len(self.plans) != 1 else ''} planned, nothing sent or written"
        if self.state == "skipped":
            return f"skipped {self.source} {self.reason}"
        return f"failed {self.source} {self.error}"


def actor_label(actor: User | str | None) -> str:
    return actor.email if isinstance(actor, User) else str(actor or JOB_ACTOR)


def window(source: str, *, since: date | None = None, today: date | None = None) -> tuple[date, date]:
    """The days a normal run re-pulls: the collector's window_days back to today (London), or from --since."""
    today = today or today_london()
    cls = COLLECTORS[source]
    start = since or (today - timedelta(days=cls.window_days - 1))
    return start, today


def next_due(tier: str, now: datetime | None = None) -> datetime:
    """When the cron next runs the tier: hourly at :07, daily at 06:10 UTC (render.yaml)."""
    now = now or utcnow()
    if tier == "hourly":
        due = now.replace(minute=7, second=0, microsecond=0)
        return due if due > now else due + timedelta(hours=1)
    due = now.replace(hour=6, minute=10, second=0, microsecond=0)
    return due if due > now else due + timedelta(days=1)


def source_row(db: Session, source: str) -> MetricSource:
    row = db.get(MetricSource, source)
    if row is None:
        row = MetricSource(source=source, status="never_run", tier=SOURCE_INFO[source]["tier"],
                           credential_env=list(env_names(source)), config={})
        db.add(row)
    return row


def run_source(db: Session, source: str, *, tier: str | None = None, since: date | None = None, until: date | None = None,
               dry_run: bool = False, actor: User | str | None = None) -> SourceRun:
    cls = COLLECTORS[source]
    own_tier = SOURCE_INFO[source]["tier"]
    tier = tier if tier in ("hourly", "daily") else own_tier
    start, end = window(source, since=since)
    if until is not None:
        end = until
    dry = source_dry_run(dry_run)
    outcome = SourceRun(source=source, tier=tier, since=start, until=end)
    collector = cls.from_config(dry_run=dry)
    began = time.monotonic()
    started_at = utcnow()
    rows: list = []
    replace: list = []
    try:
        if not dry and not collector.configured:
            raise ConnectorError("not_configured", detail="missing in this environment: " + ", ".join(collector.missing_env()))
        result = collector.pull(start, end, tier=tier)
        rows, outcome.plans, outcome.notes, replace = result.rows, result.plans, result.notes, result.replace
    except ConnectorError as exc:
        outcome.state, outcome.reason = "failed", exc.reason
        outcome.error = scrub(exc.summary(), collector.secrets())[:500]
        outcome.plans = list(collector.plans)
    except Exception as exc:   # noqa: BLE001  (a collector bug must fail its own source, never the whole run)
        outcome.state, outcome.reason = "failed", "error"
        outcome.error = scrub(f"{type(exc).__name__}: {exc}", collector.secrets())[:500]
        outcome.plans = list(collector.plans)
    finally:
        collector.close()

    if outcome.state == "ok" and dry:
        outcome.state = "dry_run"
    if outcome.state == "ok":
        try:
            outcome.rows = store.write(db, source, rows, replace)
        except Exception as exc:   # noqa: BLE001  (a refused row rolls back this source's rows, nothing else)
            db.rollback()
            outcome.state, outcome.reason, outcome.rows = "failed", "store_refused", 0
            outcome.error = scrub(f"store refused the rows: {exc}", collector.secrets())[:500]
    outcome.seconds = time.monotonic() - began

    try:
        outcome.run_id = _record(db, source, tier, own_tier, outcome, dry, started_at, actor)
    except Exception as exc:   # noqa: BLE001  (the run must still be recorded and the job must go on)
        db.rollback()
        earlier = outcome.error if outcome.state == "failed" else ""
        outcome.rows = 0
        outcome.state, outcome.reason = "failed", "record_failed"
        outcome.error = scrub(f"the run could not be recorded ({type(exc).__name__}); its rows were rolled back"
                              + (f"; before that: {earlier}" if earlier else ""), collector.secrets())[:500]
        try:
            outcome.run_id = _record(db, source, tier, own_tier, outcome, dry, started_at, actor, plans=False)
        except Exception:   # noqa: BLE001
            db.rollback()
    return outcome


def _record(db: Session, source: str, tier: str, own_tier: str, outcome: SourceRun, dry: bool, started_at: datetime,
            actor: User | str | None, *, plans: bool = True) -> int:
    """The metric_runs row and, for a real run on the source's own tier, the health row; then commit."""
    run = MetricRun(source=source, tier=tier, started_at=started_at, finished_at=utcnow(),
                    ok=outcome.state in ("ok", "dry_run"), rows=outcome.rows, error=scrub(outcome.error)[:500],
                    dry_run=dry, actor=actor_label(actor)[:200],
                    plan=outcome.plans[:PLAN_LIMIT] if dry and plans else None)
    db.add(run)
    if not dry and tier == own_tier:
        health = source_row(db, source)
        health.last_run_at = run.finished_at
        health.last_rows = outcome.rows
        health.next_due_at = next_due(health.tier)
        if outcome.state == "ok":
            health.status, health.last_ok_at, health.last_error = "ok", run.finished_at, ""
        else:
            health.status = "not_configured" if outcome.reason == "not_configured" else "failing"
            health.last_error = scrub(outcome.error)[:500]
    db.commit()
    return run.id

