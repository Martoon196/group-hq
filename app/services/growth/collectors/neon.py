"""Neon: what each database costs to run, from Neon's consumption API, per project per day.

    GET https://console.neon.tech/api/v2/projects?org_id=<NEON_ORG_ID>&limit=400&cursor=      project names
    GET https://console.neon.tech/api/v2/consumption_history/v2/projects
        ?org_id=&from=<RFC 3339>&to=<RFC 3339>&granularity=daily&limit=100&cursor=
        &metrics=compute_unit_seconds,root_branch_bytes_month,child_branch_bytes_month,instant_restore_bytes_month,
                 public_network_transfer_bytes,private_network_transfer_bytes,extra_branches_month,
                 snapshot_storage_bytes_month
    -> {"projects": [{"project_id", "periods": [{"period_id", "period_plan", "period_start", "period_end"?,
                       "consumption": [{"timeframe_start", "timeframe_end", "metrics": [{"metric_name", "value"}]}]}]}],
        "pagination": {"cursor"}}

(Neon API reference, getConsumptionHistoryPerProjectV2: every billable metric, invoice-aligned; daily granularity is
limited to the last 60 days; the call does not wake a compute.) Auth: Bearer NEON_API_KEY, an ORGANISATION key. That
key can change and delete projects, so it lives only on the web service and the daily growth cron, is never logged,
stored or shown, and every plan reads "Bearer [redacted]".

Rows: neon_<metric_name> per day per project, in Neon's own units (CU-seconds, byte-months, bytes, branch-hours),
product group, dimension project:<project_id>, meta {project_id, label, project_name} as they were at the pull. The
key never carries the label: NEON_PROJECT_PRODUCTS ("projectId=pp,...") is the founder's to edit, and a project
relabelled between two pulls must stay one row per day, not two. The screens resolve each project's label at read
time from the current NEON_PROJECT_PRODUCTS (reports.neon_label): free text, where pp, kp, ordris and hnh show the
product's name, grouphq shows Group HQ, any other label shows as typed, and a project with no label shows its Neon
name. No project is ever dropped. The Costs screen prices the rows with the rates the founder sets on Settings.
Days are Neon's: UTC calendar days (timeframe_start).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from .... import config
from ...connectors.base import ConnectorError, ConnectorResult
from ..registry import NEON_BY_API, NEON_UNITS
from .base import MAX_PAGES, MetricCollector, PullResult, SnapshotRow, number, parse_instant, today_london

API = "https://console.neon.tech/api/v2"
DAILY_LIMIT_DAYS = 60


def labels(raw: str) -> dict[str, str]:
    return {project: label[:60] for project, label in config.pairs(raw).items()}


def _rfc3339(day: date) -> str:
    return datetime.combine(day, time(0), tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class NeonCollector(MetricCollector):
    source = "neon"
    env_names = ("NEON_API_KEY", "NEON_ORG_ID", "NEON_PROJECT_PRODUCTS")
    window_days = 35                 # the whole of this month and the end of the last, inside Neon's 60-day limit
    timeout_seconds = 30.0

    @classmethod
    def from_config(cls, *, dry_run: bool = False) -> "NeonCollector":
        return cls(API, config.NEON_API_KEY, dry_run=dry_run, org_id=config.NEON_ORG_ID,
                   project_labels=labels(config.NEON_PROJECT_PRODUCTS))

    def __init__(self, *args, org_id: str = "", project_labels: dict | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.org_id = (org_id or "").strip()
        self.project_labels = project_labels or {}

    @property
    def configured(self) -> bool:
        return bool(self._token and self.org_id)

    def missing_env(self) -> list[str]:
        return [name for name, value in (("NEON_API_KEY", self._token), ("NEON_ORG_ID", self.org_id)) if not value]

    def project_names(self, notes: list) -> dict[str, str]:
        names: dict[str, str] = {}
        cursor = ""
        for _ in range(MAX_PAGES):
            params = {"org_id": self.org_id or "{NEON_ORG_ID}", "limit": 400}
            if cursor:
                params["cursor"] = cursor
            body = self.get_json("/projects", params=params)
            if not isinstance(body, dict):
                return names
            projects = [p for p in body.get("projects") or [] if isinstance(p, dict)]
            for project in projects:
                names[str(project.get("id"))] = str(project.get("name") or "")[:120]
            next_cursor = str((body.get("pagination") or {}).get("cursor") or "")
            if not projects or not next_cursor or next_cursor == cursor:
                return names
            cursor = next_cursor
        notes.append(f"projects: stopped after {MAX_PAGES} pages")
        return names

    def consumption(self, since: date, until: date, notes: list) -> list[dict]:
        out: list[dict] = []
        cursor = ""
        end = min(datetime.combine(until + timedelta(days=1), time(0), tzinfo=timezone.utc), datetime.now(timezone.utc))
        for _ in range(MAX_PAGES):
            params = {"org_id": self.org_id or "{NEON_ORG_ID}", "from": _rfc3339(since),
                      "to": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "granularity": "daily",
                      "metrics": ",".join(u.api_name for u in NEON_UNITS), "limit": 100}
            if cursor:
                params["cursor"] = cursor
            body = self.get_json("/consumption_history/v2/projects", params=params)
            if body is None:
                return out
            if not isinstance(body, dict) or not isinstance(body.get("projects"), list):
                raise ConnectorError("bad_response", detail="neon: consumption answer has no projects list")
            projects = [p for p in body["projects"] if isinstance(p, dict)]
            out.extend(projects)
            next_cursor = str((body.get("pagination") or {}).get("cursor") or "")
            if not projects or not next_cursor or next_cursor == cursor:
                return out
            cursor = next_cursor
        notes.append(f"consumption: stopped after {MAX_PAGES} pages")
        return out

    def rows_for(self, projects: list[dict], names: dict[str, str], notes: list) -> list[SnapshotRow]:
        sums: dict[tuple[date, str, str], Decimal] = defaultdict(Decimal)
        unknown: set[str] = set()
        for project in projects:
            project_id = str(project.get("project_id") or "")
            if not project_id:
                continue
            for period in project.get("periods") or []:
                for frame in (period or {}).get("consumption") or []:
                    start = parse_instant(str((frame or {}).get("timeframe_start") or ""))
                    if start is None:
                        continue
                    for entry in frame.get("metrics") or []:
                        unit = NEON_BY_API.get(str((entry or {}).get("metric_name") or ""))
                        value = number((entry or {}).get("value"))
                        if unit is None:
                            unknown.add(str((entry or {}).get("metric_name"))[:60])
                            continue
                        if value is not None:
                            sums[(start.date(), project_id, unit.metric)] += value
        if unknown:
            notes.append("Neon returned metrics Group HQ does not price yet: " + ", ".join(sorted(unknown)))
        rows = []
        for (day, project_id, metric), value in sorted(sums.items()):
            label = self.project_labels.get(project_id, "")
            meta = {"project_id": project_id, "label": label, "project_name": names.get(project_id, "")}
            rows.append(SnapshotRow(day, "group", metric, value, f"project:{project_id}", meta))
        return rows

    def pull(self, since: date, until: date, *, tier: str = "daily") -> PullResult:
        notes: list = []
        earliest = today_london() - timedelta(days=DAILY_LIMIT_DAYS - 1)
        if since < earliest:
            notes.append(f"Neon keeps daily figures for {DAILY_LIMIT_DAYS} days: pulled from {earliest} instead of {since}")
            since = earliest
        names = self.project_names(notes)
        projects = self.consumption(since, until, notes)
        rows = self.rows_for(projects, names, notes)
        return self.result(rows, notes)

    def probe(self) -> ConnectorResult:
        result = self.request("GET", "/projects", params={"org_id": self.org_id or "{NEON_ORG_ID}", "limit": 1})
        if not result.dry_run and isinstance(result.data, dict):
            result.data = {"ok": True, "projects_listed": len(result.data.get("projects") or [])}
        return result
