"""Growth: the metrics tables, the collectors, the job and the screens.

Covers: the migration head and its vocabularies (the sales_attribution view's rules are the Python ones); the metric
registry (no collector writes a name it does not register); upserts are idempotent (collect twice, same rows) and a
long dimension is cut before the key; every collector's dry-run plan, every credential header redacted and no secret
anywhere in it; every collector's recorded fixture parsed into exact rows (products, Stripe with the MRR maths, Brevo,
Zernio, Clerk, GA4 with its thresholding flag and a service-account token minted through httpx, Search Console with
ctr and position recomputed, Neon per project with free-text labels, the CSV import); the sales attribution
precedence (promo beats UTM beats affiliate beats first touch beats direct); the job's exit codes (failed source,
not-configured source, off source, schema not at head, dry run); the screens for an admin and an anonymous caller;
the Test, Pull now and switch buttons, the promo map and the CSV import, each with its audit row; the running-cost
rates (blank until set, never invented); secrets absent from every stored plan, audit row and page.

Every platform answer is a recorded fixture under tests/fixtures/growth/: nothing here reaches the network.
"""
from __future__ import annotations

import importlib.util
import io
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import text

from app import config, migrate, models, seed
from app.jobs import growth_pull as job
from app.models import (AdSpend, AppSetting, AuditLog, MetricRun, MetricSnapshot, MetricSource, ProductPurchase,
                        PromoChannelMap)
from app.services import settings
from app.services.growth import attribution, registry, reports, store
from app.services.growth.collectors import COLLECTORS, SOURCE_INFO, csv_import, source_mode
from app.services.growth.collectors.base import Replace, SnapshotRow, add_up, scrub
from app.services.growth.collectors.brevo import BrevoCollector
from app.services.growth.collectors.clerk import ClerkOrdrisCollector
from app.services.growth.collectors.ga4 import GA4Collector
from app.services.growth.collectors.neon import NeonCollector
from app.services.growth.collectors.products import ProductHNHCollector, ProductPPCollector
from app.services.growth.collectors.search_console import SearchConsoleCollector, page_key
from app.services.growth.collectors.stripe_read import StripeMH196Collector, monthly_pence, subscription_mrr
from app.services.growth.collectors.zernio import ZernioCollector
from app.services.growth.runner import run_source
from tests.conftest import ADMIN_EMAIL, session
from tests.test_people import flash_of

FIX = Path(__file__).parent / "fixtures" / "growth"
SINCE, UNTIL = date(2026, 10, 19), date(2026, 10, 22)
D20, D21, D22 = date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22)
PP = "https://pp.example.test"
HNH = "https://hnh.example.test"
STRIPE = "https://api.stripe.com"
BREVO = "https://api.brevo.com/v3"
ZERNIO = "https://zernio.com/api/v1"
NEON = "https://console.neon.tech/api/v2"
GA4 = "https://analyticsdata.googleapis.com/v1beta"
GSC = "https://www.googleapis.com/webmasters/v3"
TOKEN_URI = "https://oauth2.googleapis.com/token"
# Test credentials: shaped like nothing real, so no scanner mistakes them for a key; the scrub test builds real-looking
# ones at run time instead.
SECRETS = {
    "STRIPE_RK_MH196": "test-stripe-mh196-restricted-not-real",
    "STRIPE_RK_HNH": "test-stripe-hnh-restricted-not-real",
    "BREVO_API_KEY_HQ": "test-brevo-key-not-real",
    "ZERNIO_API_KEY": "test-zernio-key-not-real",
    "CLERK_SECRET_KEY_ORDRIS": "test-clerk-key-not-real",
    "NEON_API_KEY": "test-neon-org-key-not-real",
}
_SA: dict = {}


def fx(name: str):
    return json.loads((FIX / name).read_text())


def ok(name: str) -> httpx.Response:
    return httpx.Response(200, json=fx(name))


def service_account() -> dict:
    """A service-account key file made for this run (a real RSA key, so google-auth really signs), never committed."""
    if not _SA:
        import rsa
        _, private = rsa.newkeys(1024)
        _SA.update({"type": "service_account", "project_id": "growth-test", "private_key_id": "test-key-id-0001",
                    "private_key": private.save_pkcs1().decode(), "client_email": "hq@growth-test.iam.gserviceaccount.com",
                    "client_id": "1", "token_uri": "https://example.invalid/token"})
    return dict(_SA)


@pytest.fixture
def creds(monkeypatch):
    """Every growth credential and mapping set to test values (conftest already holds the product tokens)."""
    for name, value in SECRETS.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, "GOOGLE_SA_JSON", json.dumps(service_account()))
    monkeypatch.setattr(config, "GA4_PROPERTY_IDS", "pp=111111111")
    monkeypatch.setattr(config, "GSC_SITES", "pp=sc-domain:propertypredator.com")
    monkeypatch.setattr(config, "STRIPE_PRODUCT_MAP", "prod_pp=pp")
    monkeypatch.setattr(config, "BREVO_LIST_PRODUCTS", "3=kp,7=pp")
    monkeypatch.setattr(config, "ZERNIO_PROFILE_PRODUCTS", "prof1=pp")
    monkeypatch.setattr(config, "ZERNIO_AD_ACCOUNTS", "adacc1=tiktok")
    monkeypatch.setattr(config, "NEON_ORG_ID", "org-test-123")
    monkeypatch.setattr(config, "NEON_PROJECT_PRODUCTS", "proj-pp-111=pp,proj-hq-222=grouphq,proj-alpha-333=AlphaStack")
    return SECRETS


@pytest.fixture
def live(monkeypatch):
    """Reads really go out (to respx): conftest forces CONNECTORS_DRY_RUN=1, the floor."""
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN", False)


def every_secret() -> list[str]:
    sa = service_account()
    return list(SECRETS.values()) + [sa["private_key"].splitlines()[1], "ya29.test-access-token-not-real",
                                     "test-pp_service_token", "test-hnh_partner_token"]


def keys(rows) -> dict:
    return {(r.date, r.product, r.metric, r.dimension): Decimal(r.value) for r in rows if isinstance(r, SnapshotRow)}


def switch_on(*sources: str) -> None:
    with session() as db:
        for source in sources:
            settings.set_growth_source_switch(db, source, "on", actor=ADMIN_EMAIL)
        db.commit()


# ---- schema ----------------------------------------------------------------------------------------------------
def test_the_database_is_at_the_growth_head():
    """At the head, and the head is at or after 0007_growth (later packets add their own revisions on top)."""
    from alembic.script import ScriptDirectory
    assert migrate.current_revision() == migrate.head_revision()
    script = ScriptDirectory.from_config(migrate.alembic_config())
    assert "0007_growth" in {rev.revision for rev in script.walk_revisions()}


def _migration():
    path = Path(config.ROOT) / "alembic" / "versions" / "0007_growth.py"
    spec = importlib.util.spec_from_file_location("migration_0007_growth", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_mirrors_the_models_and_the_attribution_rules():
    m = _migration()
    assert m.SOURCES == models.SOURCES and m.SOURCE_STATUSES == models.SOURCE_STATUSES
    assert m.METRIC_PRODUCTS == models.METRIC_PRODUCTS and m.AD_PLATFORMS == models.AD_PLATFORMS
    assert m.PURCHASE_KINDS == models.PURCHASE_KINDS and m.PURCHASE_STATUSES == models.PURCHASE_STATUSES
    assert m.CHANNELS == models.CHANNELS and m.PRODUCTS == models.PRODUCTS
    assert m.UTM_RULES == attribution.UTM_RULES and m.PLATFORM_ALIASES == attribution.PLATFORM_ALIASES
    assert set(SOURCE_INFO) == set(models.SOURCES)


def test_the_unique_key_and_the_vocabularies_are_enforced_by_the_database(db):
    db.add(MetricSnapshot(date=D20, source="ga4", product="pp", metric="visitors", dimension="", value=1))
    db.commit()
    db.add(MetricSnapshot(date=D20, source="ga4", product="pp", metric="visitors", dimension="", value=2))
    with pytest.raises(Exception):
        db.commit()
    db.rollback()
    db.add(MetricSnapshot(date=D20, source="nowhere", product="pp", metric="visitors", dimension="", value=1))
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


def test_metric_sources_are_seeded_once_and_never_overwritten(db):
    assert seed.ensure_metric_sources(db) == len(models.SOURCES)
    db.commit()
    rows = {r.source: r for r in db.query(MetricSource).all()}
    assert rows["neon"].status == "off" and rows["neon"].credential_env == ["NEON_API_KEY", "NEON_ORG_ID", "NEON_PROJECT_PRODUCTS"]
    assert rows["linkedin"].status == "not_configured" and "approval" in rows["linkedin"].config["note"]
    assert rows["stripe_hnh"].status == "not_configured"
    assert rows["product_pp"].tier == "hourly" and rows["ga4"].tier == "daily"
    rows["neon"].config = {"note": "edited"}
    db.commit()
    assert seed.ensure_metric_sources(db) == 0
    assert seed.seed(db)["metric_sources_added"] == 0
    db.expire_all()
    assert db.get(MetricSource, "neon").config == {"note": "edited"}


# ---- registry and store ----------------------------------------------------------------------------------------
def test_the_registry_is_consistent():
    names = [m.name for m in registry.METRICS]
    assert len(names) == len(set(names))
    for metric in registry.METRICS:
        assert set(metric.sources) <= set(models.SOURCES), metric.name
        assert (metric.unit == "pence") == metric.name.endswith("_pence"), metric.name
    for unit in registry.NEON_UNITS:
        assert registry.is_registered(unit.metric, "neon")


def test_the_store_refuses_a_metric_the_registry_does_not_know(db):
    with pytest.raises(ValueError, match="not in the registry"):
        store.upsert_snapshots(db, "ga4", [SnapshotRow(D20, "pp", "visiters", 3)])
    with pytest.raises(ValueError, match="not in the registry"):
        store.upsert_snapshots(db, "brevo", [SnapshotRow(D20, "pp", "visitors", 3)])     # a source writes only its own
    with pytest.raises(ValueError, match="unknown product"):
        store.upsert_snapshots(db, "ga4", [SnapshotRow(D20, "xx", "visitors", 3)])
    db.rollback()
    assert db.query(MetricSnapshot).count() == 0


def test_an_upsert_twice_leaves_one_row_and_the_newest_value(db):
    long_query = "query:" + "hmo calculator " * 30
    rows = [SnapshotRow(D20, "pp", "search_clicks", 3, long_query), SnapshotRow(D20, "pp", "search_clicks", 4, long_query + " more")]
    with pytest.raises(ValueError, match="must add them up"):
        store.upsert_snapshots(db, "search_console", rows)       # never a silent last-wins: 3 or 4 would be lost
    assert store.upsert_snapshots(db, "search_console", add_up(rows)) == 1, "two values that share their first 200 characters are one row"
    db.commit()
    [row] = db.query(MetricSnapshot).all()
    assert row.value == Decimal("7.0000") and len(row.dimension) == 200, "added up, not overwritten"
    store.upsert_snapshots(db, "search_console", [SnapshotRow(D20, "pp", "search_clicks", 9, long_query)] * 2)
    db.commit()
    db.expire_all()
    [row] = db.query(MetricSnapshot).all()
    assert row.value == Decimal("9.0000"), "an identical repeat is one row, and a re-pull's newest value wins"
    with pytest.raises(ValueError, match="cannot be added up"):
        add_up([SnapshotRow(D20, "pp", "search_ctr", Decimal("0.1"), "page:/a")] * 2)


# ---- dry run: every collector plans, nothing is sent, nothing secret is planned -----------------------------------
CREDENTIAL_HEADERS = {"Authorization", "X-Service-Token", "api-key"}


@pytest.mark.parametrize("source", sorted(COLLECTORS))
def test_every_collector_dry_run_plans_with_every_credential_redacted(source, creds):
    collector = COLLECTORS[source].from_config(dry_run=True)
    with respx.mock(assert_all_called=False) as mock:
        results = [collector.pull(SINCE, UNTIL, tier=tier) for tier in COLLECTORS[source].tiers]
        probe = COLLECTORS[source].from_config(dry_run=True).probe()
        assert mock.calls.call_count == 0, "a dry run reaches nothing"
    assert probe.dry_run and probe.plan
    for result in results:
        assert result.rows == [] and result.plans, f"{source} planned nothing"
        dumped = json.dumps(result.plans, default=str)
        for secret in every_secret():
            assert secret not in dumped, f"{source} planned a credential"
        for plan in result.plans:
            for name, value in (plan.get("headers") or {}).items():
                if name in CREDENTIAL_HEADERS:
                    assert value in ("Bearer [redacted]", "[redacted]"), f"{source} {name}: {value}"
        assert any((p.get("headers") or {}) for p in result.plans), f"{source}: no request carried a header"


def test_scrub_blanks_keys_a_platform_might_echo():
    stripe_key = "rk" + "_live_" + "A1b2C3d4E5f6"
    brevo_key = "xkeysib" + "-0123abcd-ffff"
    text_in = f"Invalid API Key provided: {stripe_key}; also {brevo_key}; Authorization: Bearer abc.def; mine my-own-secret"
    out = scrub(text_in, ("my-own-secret",))
    for leaked in (stripe_key, brevo_key, "abc.def", "my-own-secret"):
        assert leaked not in out
    assert out.count("[redacted]") == 4


# ---- products -------------------------------------------------------------------------------------------------
def test_the_product_collector_parses_metrics_and_purchases_with_pp_s_own_headers(creds, live):
    with respx.mock(base_url=PP) as mock:
        metrics = mock.get("/api/partner/grouphq/metrics").mock(return_value=ok("product_pp_metrics.json"))
        purchases = mock.get("/api/partner/grouphq/purchases").mock(
            side_effect=[ok("product_pp_purchases_1.json"), ok("product_pp_purchases_2.json")])
        result = ProductPPCollector.from_config().pull(SINCE, UNTIL)
    request = metrics.calls[0].request
    assert request.headers["X-Service-Token"] == "test-pp_service_token" and request.headers["X-Partner-Client"] == "grouphq"
    assert dict(request.url.params) == {"from": "2026-10-19", "to": "2026-10-22"}
    assert purchases.calls[1].request.url.params["cursor"] == "c2"
    snapshots = keys(result.rows)
    assert len(snapshots) == 24
    assert snapshots[(D20, "pp", "sign_ups", "")] == 12
    assert snapshots[(D20, "pp", "sign_ups", "method:google")] == 3
    assert snapshots[(D20, "pp", "leads", "kind:valuation")] == 3
    assert snapshots[(D20, "pp", "mrr_pence", "")] == 68900
    assert snapshots[(D20, "pp", "promo_redemptions", "promo:PPMETA")] == 1
    assert snapshots[(D21, "pp", "promo_redemptions", "promo:PPGOOGLE")] == 1, "codes are upper case"
    assert (D21, "pp", "leads", "") not in snapshots, "a null is not a zero: nothing is written"
    assert (D21, "pp", "trials_started", "") not in snapshots, "a metric missing from a day is omitted"
    bought = {r.purchase_ref: r for r in result.rows if not isinstance(r, SnapshotRow)}
    assert set(bought) == {"cs_test_a1", "cs_test_a2"}, "an unknown kind is skipped, not guessed"
    a1, a2 = bought["cs_test_a1"], bought["cs_test_a2"]
    assert a1.promo_code == "PPMETA" and a1.account_key == "ab" * 32 and a1.occurred_at == datetime(2026, 10, 20, 10, 15)
    assert a1.first_touch["utm_campaign"] == "pp-launch-202611-hmo" and "fbclid" not in a1.first_touch
    assert "email" not in json.dumps(a1.raw) and "buyer@example.test" not in json.dumps(a1.raw)
    assert a2.account_key == "", "a key that is not a sha256 digest is dropped, so an email can never land here"
    assert a2.occurred_at == datetime(2026, 10, 21, 8, 0) and a2.affiliate_ref == "aff-77"
    assert any("not a sha256 digest" in n for n in result.notes) and any("mystery" in n for n in result.notes)


def test_hello_new_home_purchases_are_its_instructions_with_its_bearer_header(creds, live):
    with respx.mock(base_url=HNH) as mock:
        metrics = mock.get("/api/partner/metrics").mock(return_value=ok("product_hnh_metrics.json"))
        mock.get("/api/partner/instructions").mock(return_value=ok("product_hnh_instructions.json"))
        result = ProductHNHCollector.from_config().pull(SINCE, UNTIL)
    assert metrics.calls[0].request.headers["Authorization"] == "Bearer test-hnh_partner_token"
    [purchase] = [r for r in result.rows if not isinstance(r, SnapshotRow)]
    assert purchase.kind == "instruction" and purchase.product == "hnh" and purchase.purchase_ref == "INS-1001"
    assert keys(result.rows)[(D20, "hnh", "leads", "kind:landlord")] == 3


def test_a_product_answering_for_another_product_is_refused(creds, live):
    body = fx("product_pp_metrics.json") | {"product": "kp"}
    with respx.mock(base_url=PP) as mock:
        mock.get("/api/partner/grouphq/metrics").mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(Exception, match="answered for product"):
            ProductPPCollector.from_config().pull(SINCE, UNTIL)


# ---- Stripe ---------------------------------------------------------------------------------------------------
def test_mrr_normalises_every_interval_and_both_kinds_of_discount():
    assert monthly_pence(1900, 1, "month") == Decimal(1900)
    assert monthly_pence(1900, 1, "month", 3) == Decimal(1900) / 3
    assert monthly_pence(19000, 1, "year") == Decimal(19000) / 12
    assert monthly_pence(500, 2, "week") == Decimal(1000) * 52 / 12
    assert monthly_pence(100, 1, "day") == Decimal(100) * 365 / 12
    assert monthly_pence(None, 1, "month") is None, "a tiered price has no fixed amount"
    now = int(datetime(2026, 10, 22).timestamp())
    [sub_1, sub_2, sub_3] = fx("stripe_subscriptions_1.json")["data"]
    coupons = {"ORD1": fx("stripe_coupon_ORD1.json")}
    assert subscription_mrr(sub_1, now) == Decimal(1900)
    assert subscription_mrr(sub_2, now).quantize(Decimal("0.0001")) == Decimal("1266.6667"), "19000 a year, 20% off (source.coupon expanded)"
    assert subscription_mrr(sub_3, now, coupons=coupons).quantize(Decimal("0.0001")) == Decimal("3900.0000"), \
        "1000 a week less 100 a week off (source.coupon an id, read from /v1/coupons)"
    unread: list = []
    assert subscription_mrr(sub_3, now, unread) == Decimal(1000) * 52 / 12, "a coupon not read is not guessed"
    assert any("without its coupon" in n for n in unread)
    legacy = dict(sub_2, discounts=[{"id": "di_old", "object": "discount", "end": None,
                                     "coupon": {"id": "PROMO20", "percent_off": 20}}])      # before 2025-09-30.clover
    assert subscription_mrr(legacy, now) == subscription_mrr(sub_2, now), "the older shape still applies"
    clover_id = {"id": "sub_x", "status": "active", "discounts": [{"id": "di_1", "object": "discount", "end": None,
                                                                    "source": {"type": "coupon", "coupon": "PROMO20"}}],
                 "items": {"data": [{"quantity": 1, "price": {"unit_amount": 1900, "currency": "gbp",
                                                               "recurring": {"interval": "month", "interval_count": 1}}}]}}
    assert subscription_mrr(clover_id, now, coupons={"PROMO20": {"id": "PROMO20", "percent_off": 20}}) == Decimal(1520)
    expired = dict(sub_2, discounts=[dict(sub_2["discounts"][0], end=now - 60)])
    assert subscription_mrr(expired, now).quantize(Decimal("0.0001")) == Decimal("1583.3333"), "an ended discount is not applied"
    notes: list = []
    sub_6 = fx("stripe_subscriptions_2.json")["data"][2]
    assert subscription_mrr(sub_6, now, notes) == 0, "metered and non-GBP items are left out"
    assert any("usd" in n for n in notes)


def test_stripe_parses_subscriptions_sessions_balance_and_codes_into_exact_rows(creds, live):
    with respx.mock(base_url=STRIPE) as mock:
        subs = mock.get("/v1/subscriptions").mock(side_effect=[ok("stripe_subscriptions_1.json"), ok("stripe_subscriptions_2.json")])
        mock.get("/v1/checkout/sessions").mock(return_value=ok("stripe_checkout_sessions.json"))
        mock.get("/v1/balance_transactions").mock(return_value=ok("stripe_balance_transactions.json"))
        mock.get("/v1/promotion_codes").mock(return_value=ok("stripe_promotion_codes.json"))
        coupon = mock.get("/v1/coupons/ORD1").mock(return_value=ok("stripe_coupon_ORD1.json"))
        collector = StripeMH196Collector.from_config()
        result = collector.pull(SINCE, UNTIL)
    assert subs.calls[0].request.headers["Authorization"] == f"Bearer {SECRETS['STRIPE_RK_MH196']}"
    assert subs.calls[0].request.headers["Stripe-Version"] == "2025-09-30.clover", "the shapes never follow the account default"
    assert subs.calls[0].request.url.params["expand[]"] == "data.discounts.source.coupon"
    assert coupon.call_count == 1 and coupon.calls[0].request.headers["Stripe-Version"] == "2025-09-30.clover"
    assert subs.calls[0].request.url.params["status"] == "all"
    assert subs.calls[1].request.url.params["starting_after"] == "sub_3"
    rows = keys(result.rows)
    today = reports.today_london()
    assert len(rows) == 72
    assert rows[(today, "pp", "paying_active", "")] == 2 and rows[(today, "pp", "mrr_pence", "")] == 3167
    assert rows[(today, "ordris", "paying_active", "")] == 1 and rows[(today, "ordris", "mrr_pence", "")] == 3900
    assert rows[(today, "ordris", "trials_active", "")] == 1
    assert rows[(today, "kp", "paying_active", "")] == 1 and rows[(today, "kp", "mrr_pence", "")] == 0
    assert rows[(today, "group", "mrr_pence", "")] == 0
    assert rows[(D20, "pp", "paying_new", "")] == 1 and rows[(D21, "pp", "paying_new", "")] == 0
    assert rows[(D21, "pp", "cancelled", "")] == 1
    assert rows[(D20, "pp", "purchases", "")] == 1 and rows[(D20, "pp", "purchases", "promo:PPMETA")] == 1
    assert rows[(D20, "pp", "purchases", "campaign:pp-launch-202611-hmo")] == 1
    assert rows[(D21, "ordris", "purchases", "")] == 1, "a free-trial checkout is still a completed checkout"
    assert rows[(D20, "group", "revenue_pence", "")] == 1900 and rows[(D21, "group", "revenue_pence", "")] == 3900
    assert rows[(D21, "group", "refunds_pence", "")] == 1900
    assert rows[(today, "ordris", "promo_times_redeemed", "promo:ORDMETA")] == 3
    assert rows[(today, "group", "promo_times_redeemed", "promo:KPGOOGLE")] == 0
    [mrr] = [r for r in result.rows if r.metric == "mrr_pence" and r.product == "pp"]
    assert mrr.meta == {"basis": "net_of_discounts"}
    assert "buyer@example.test" not in json.dumps([r.__dict__ for r in result.rows], default=str)
    assert any("only GBP" in n for n in result.notes)


# ---- Brevo, Zernio, Clerk ---------------------------------------------------------------------------------------
def test_brevo_counts_lists_contacts_campaigns_and_transactional_without_keeping_a_person(creds, live):
    with respx.mock(base_url=BREVO) as mock:
        lists = mock.get("/contacts/lists").mock(return_value=ok("brevo_lists.json"))
        mock.get("/contacts").mock(return_value=ok("brevo_contacts.json"))
        mock.get("/emailCampaigns").mock(return_value=ok("brevo_campaigns.json"))
        mock.get("/smtp/statistics/reports").mock(return_value=ok("brevo_smtp_reports.json"))
        result = BrevoCollector.from_config().pull(SINCE, UNTIL)
    assert lists.calls[0].request.headers["api-key"] == SECRETS["BREVO_API_KEY_HQ"]
    rows = keys(result.rows)
    today = reports.today_london()
    assert rows[(today, "kp", "email_contacts", "list:KPUK Investors")] == 412
    assert rows[(today, "pp", "email_contacts", "list:PP launch")] == 55
    assert rows[(D20, "kp", "email_contacts_new", "")] == 1
    assert rows[(D22, "pp", "email_contacts_new", "")] == 1, "23:30 UTC on the 21st is the 22nd in London (BST)"
    assert rows[(D21, "group", "email_contacts_new", "")] == 1
    assert rows[(D20, "kp", "email_sent", "campaign:kp-launch-202611-brief")] == 400
    assert rows[(D20, "kp", "email_bounces", "campaign:kp-launch-202611-brief")] == 4
    assert rows[(D20, "kp", "email_opens_unique", "campaign:kp-launch-202611-brief")] == 180
    assert rows[(D21, "pp", "email_spam_reports", "campaign:October update")] == 1, "no prefix: the list map decides"
    assert not any(k[3] == "campaign:Summer note" for k in rows), "a campaign older than the look-back is not re-pulled"
    assert rows[(D20, "group", "email_sent", "stream:transactional")] == 50
    assert rows[(D20, "group", "email_bounces", "stream:transactional")] == 1
    assert "@example.test" not in json.dumps([r.__dict__ for r in result.rows], default=str)


def test_zernio_writes_daily_totals_followers_posts_and_ad_spend(creds, live):
    with respx.mock(base_url=ZERNIO) as mock:
        daily = mock.get("/analytics/daily-metrics").mock(return_value=ok("zernio_daily_metrics.json"))
        mock.get("/accounts/follower-stats").mock(return_value=ok("zernio_follower_stats.json"))
        mock.get("/analytics").mock(return_value=ok("zernio_posts.json"))
        mock.get("/ads/timeline").mock(return_value=ok("zernio_ads_timeline.json"))
        result = ZernioCollector.from_config().pull(SINCE, UNTIL)
    assert daily.calls[0].request.url.params["attribution"] == "received"
    rows = keys(result.rows)
    assert rows[(D20, "pp", "posts", "")] == 2 and rows[(D20, "pp", "post_impressions", "")] == 1200
    assert rows[(D20, "pp", "post_engagements", "")] == 50, "likes, comments, shares and saves"
    assert rows[(D21, "pp", "video_views", "")] == 80
    assert rows[(D21, "pp", "followers", "account:instagram:propertypredator")] == 1012
    assert rows[(D21, "pp", "followers_new", "account:instagram:propertypredator")] == 12
    assert (D21, "pp", "followers_new", "account:tiktok:ukpropertyinvestment") not in rows, "one point has no change"
    assert rows[(D20, "pp", "post_engagements", "post:zernio:p1")] == 37
    assert not any(k[3] == "post:zernio:p3" for k in rows), "a promoted post is left out of organic figures"
    [p2] = [r for r in result.rows if isinstance(r, SnapshotRow) and r.dimension == "post:zernio:p2" and r.metric == "video_views"]
    assert p2.meta["external"] is True and p2.meta["url"].startswith("https://www.tiktok.com/")
    [spend] = [r for r in result.rows if not isinstance(r, SnapshotRow)]
    assert (spend.platform, spend.spend_pence, spend.clicks, spend.raw["via"]) == ("tiktok", 1234, 30, "zernio")
    assert any("USD" in n for n in result.notes)


def test_clerk_counts_ordris_sign_ups_per_london_day(creds, live):
    with respx.mock() as mock:
        route = mock.get("https://api.clerk.com/v1/users/count").mock(return_value=ok("clerk_users_count.json"))
        result = ClerkOrdrisCollector.from_config().pull(D20, D21)
    assert route.call_count == 2
    after, before = int(route.calls[0].request.url.params["created_at_after"]), int(route.calls[0].request.url.params["created_at_before"])
    assert before - after == 24 * 3600 * 1000 + 1, "midnight to midnight, London time, in milliseconds"
    assert keys(result.rows) == {(D20, "ordris", "sign_ups", ""): 4, (D21, "ordris", "sign_ups", ""): 4}


# ---- GA4 and Search Console ---------------------------------------------------------------------------------------
def test_ga4_mints_its_token_and_parses_the_four_reports(creds, live):
    with respx.mock() as mock:
        token = mock.post(TOKEN_URI).mock(return_value=ok("google_token.json"))
        batch = mock.post(f"{GA4}/properties/111111111:batchRunReports").mock(return_value=ok("ga4_batch_pp.json"))
        result = GA4Collector.from_config().pull(SINCE, UNTIL)
    assert token.call_count == 1, "the key file's own token_uri is ignored: the assertion goes only to Google"
    assert b"assertion=" in token.calls[0].request.content
    assert batch.calls[0].request.headers["Authorization"] == "Bearer ya29.test-access-token-not-real"
    body = json.loads(batch.calls[0].request.content)
    assert len(body["requests"]) == 4 and all(r["returnPropertyQuota"] for r in body["requests"])
    rows = keys(result.rows)
    assert rows[(D20, "pp", "visitors", "")] == 120 and rows[(D21, "pp", "sessions", "")] == 130
    assert rows[(D20, "pp", "revenue_pence", "")] == 1900
    assert rows[(D20, "pp", "visitors", "source_medium:meta/paid_social")] == 70
    assert rows[(D20, "pp", "sessions", "campaign:pp-launch-202611-hmo")] == 90
    assert rows[(D20, "pp", "sessions", "channel:Organic Search")] == 60
    assert (D21, "pp", "sessions", "campaign:(not set)") not in rows
    assert rows[(D20, "pp", "sign_ups_pixel", "")] == 7 and rows[(D20, "pp", "sign_ups_pixel", "source_medium:meta/paid_social")] == 5
    assert rows[(D20, "pp", "purchases_pixel", "")] == 1 and rows[(D21, "pp", "leads_pixel", "")] == 3
    assert rows[(D20, "pp", "purchases_pixel", "promo:PPMETA")] == 1 and rows[(D20, "pp", "revenue_pence", "promo:PPMETA")] == 1900
    assert rows[(reports.today_london(), "pp", "quota_remaining", "")] == 199940, "the lowest remaining of the four reports"
    flagged = {r.dimension for r in result.rows if r.meta.get("thresholded")}
    assert "source_medium:meta/paid_social" in flagged and "" not in flagged, "only the thresholded report is flagged"
    assert any("thresholded" in n for n in result.notes)


def test_ga4_on_the_hourly_tier_reads_realtime_users_only(creds, live):
    with respx.mock() as mock:
        mock.post(TOKEN_URI).mock(return_value=ok("google_token.json"))
        mock.post(f"{GA4}/properties/111111111:runRealtimeReport").mock(return_value=ok("ga4_realtime.json"))
        result = GA4Collector.from_config().pull(SINCE, UNTIL, tier="hourly")
    assert keys(result.rows) == {(reports.today_london(), "pp", "realtime_users", ""): 4}


def test_a_refused_google_token_fails_with_no_key_in_the_error(creds, live):
    with respx.mock() as mock:
        mock.post(TOKEN_URI).mock(return_value=httpx.Response(400, json={"error": "invalid_grant", "error_description": "Invalid JWT Signature."}))
        with pytest.raises(Exception) as caught:
            GA4Collector.from_config().pull(SINCE, UNTIL)
    assert caught.value.reason == "auth_failed" and "PRIVATE KEY" not in str(caught.value)


def test_search_console_pulls_five_groupings_and_reports_recompute_ctr_and_position(creds, live, db):
    with respx.mock() as mock:
        mock.post(TOKEN_URI).mock(return_value=ok("google_token.json"))
        route = mock.post(f"{GSC}/sites/sc-domain%3Apropertypredator.com/searchAnalytics/query").mock(
            side_effect=[ok("gsc_date.json"), ok("gsc_query.json"), ok("gsc_page.json"), ok("gsc_device.json"), ok("gsc_country.json")])
        result = SearchConsoleCollector.from_config().pull(SINCE, UNTIL)
    sent = [json.loads(c.request.content) for c in route.calls]
    assert [b["dimensions"] for b in sent] == [["date"], ["date", "query"], ["date", "page"], ["date", "device"], ["date", "country"]]
    assert all(b["dataState"] == "final" and b["type"] == "web" for b in sent)
    assert sent[0]["endDate"] <= (reports.today_london() - timedelta(days=3)).isoformat()
    rows = keys(result.rows)
    assert rows[(date(2026, 10, 18), "pp", "search_clicks", "page:/valuation")] == 5, "apex ?query and www added up"
    assert rows[(date(2026, 10, 18), "pp", "search_impressions", "page:/valuation")] == 120
    assert rows[(date(2026, 10, 18), "pp", "search_position", "page:/valuation")] == Decimal("6.65"), "(7.2 x 90 + 5 x 30) / 120"
    assert rows[(date(2026, 10, 18), "pp", "search_ctr", "page:/valuation")] == Decimal(5) / Decimal(120)
    assert rows[(date(2026, 10, 18), "pp", "search_clicks", "page:blog.propertypredator.com/hmo")] == 1
    assert rows[(date(2026, 10, 18), "pp", "search_impressions", "device:MOBILE")] == 300
    store.upsert_snapshots(db, "search_console", [r for r in result.rows])
    db.commit()
    period = reports.Period("30d", date(2026, 10, 18), date(2026, 10, 19), date(2026, 10, 16), date(2026, 10, 17))
    data = reports.search(db, period, "pp")
    site = data["sites"][0]["now"]
    assert site["clicks"] == "16" and site["impressions"] == "600" and site["ctr"] == "2.7%", "16 / 600, not the mean of the ratios"
    assert site["position"] == "11.0", "(12.5 x 400 + 8 x 200) / 600"
    [query] = data["queries"]
    assert query["value"] == "hmo calculator" and query["ctr"] == "1.5%" and query["position"] == "12.5", "(5 x 100 + 15 x 300) / 400"


def test_page_keys_keep_the_path_or_the_other_host():
    site = "sc-domain:propertypredator.com"
    assert page_key("https://propertypredator.com/valuation?x=1", site) == "/valuation"
    assert page_key("https://www.propertypredator.com/", site) == "/"
    assert page_key("https://blog.propertypredator.com/hmo", site) == "blog.propertypredator.com/hmo"


# ---- Neon: running costs ------------------------------------------------------------------------------------------
def test_neon_reads_every_project_with_free_text_labels_and_never_drops_one(creds, live):
    with respx.mock(base_url=NEON) as mock:
        projects = mock.get("/projects").mock(return_value=ok("neon_projects.json"))
        consumption = mock.get("/consumption_history/v2/projects").mock(
            side_effect=[ok("neon_consumption_1.json"), ok("neon_consumption_2.json"), ok("neon_consumption_3.json")])
        result = NeonCollector.from_config().pull(SINCE, UNTIL)
    first = consumption.calls[0].request
    assert first.headers["Authorization"] == f"Bearer {SECRETS['NEON_API_KEY']}"
    assert first.url.params["org_id"] == "org-test-123" and first.url.params["granularity"] == "daily"
    assert first.url.params["metrics"].split(",") == [u.api_name for u in registry.NEON_UNITS]
    assert consumption.calls[1].request.url.params["cursor"] == "proj-hq-222"
    assert projects.calls[0].request.url.params["org_id"] == "org-test-123"
    rows = keys(result.rows)
    assert {r.product for r in result.rows} == {"group"}, "a label is never part of the key: relabelling must not split a project"
    assert rows[(D20, "group", "neon_compute_unit_seconds", "project:proj-pp-111")] == 7200
    assert rows[(D20, "group", "neon_root_branch_bytes_month", "project:proj-pp-111")] == 1500000000
    assert rows[(D21, "group", "neon_extra_branches_month", "project:proj-pp-111")] == 744
    assert rows[(D20, "group", "neon_compute_unit_seconds", "project:proj-hq-222")] == 1800
    assert rows[(D20, "group", "neon_compute_unit_seconds", "project:proj-alpha-333")] == 360
    assert rows[(D21, "group", "neon_compute_unit_seconds", "project:proj-test-444")] == 36
    assert rows[(D21, "group", "neon_compute_unit_seconds", "project:proj-gone-555")] == 72, "a project the list misses is kept"
    meta = {r.dimension: r.meta for r in result.rows}
    assert meta["project:proj-alpha-333"]["label"] == "AlphaStack" and meta["project:proj-pp-111"]["label"] == "pp"
    assert meta["project:proj-test-444"] == {"project_id": "proj-test-444", "label": "", "project_name": "disposable-test"}
    assert any("future_metric_x" in n for n in result.notes), "a metric Group HQ cannot price yet is named, not dropped silently"


def test_running_costs_show_names_and_estimate_only_from_the_founder_s_rates(db, creds, live):
    with respx.mock(base_url=NEON) as mock:
        mock.get("/projects").mock(return_value=ok("neon_projects.json"))
        mock.get("/consumption_history/v2/projects").mock(
            side_effect=[ok("neon_consumption_1.json"), ok("neon_consumption_2.json"), ok("neon_consumption_3.json")])
        store.upsert_snapshots(db, "neon", NeonCollector.from_config().pull(SINCE, UNTIL).rows)
    db.commit()
    today = date(2026, 10, 23)
    blank = reports.running_costs(db, settings.running_cost_rates(db), today)
    labels = {m["label"]: m for m in blank["month"]}
    assert set(labels) == {"Property Predator", "Group HQ", "AlphaStack", "disposable-test", "proj-gone-555"}
    assert all(m["cost"] is None for m in blank["month"]) and not blank["rates_set"], "no rate, no estimate: nothing invented"
    usage = {u["label"]: u for u in labels["Property Predator"]["usage"]}
    assert usage["Compute"]["amount"] == "3.0000" and usage["Compute"]["unit"] == "CU-hour", "10,800 CU-seconds"
    assert usage["Storage, root branches"]["amount"] == "3.0000" and usage["Extra branches"]["amount"] == "1.0000"
    settings.set_running_cost_rates(db, {"neon.compute_unit_seconds": "0.106", "neon.root_branch_bytes_month": "0.35"},
                                    actor=ADMIN_EMAIL)
    db.commit()
    priced = reports.running_costs(db, settings.running_cost_rates(db), today)
    pp = {m["label"]: m for m in priced["month"]}["Property Predator"]
    assert pp["cost"] == "$1.37", "3 x 0.106 + 3 x 0.35 = 1.368"
    assert set(pp["unpriced"]) == {"Instant restore", "Public network transfer", "Extra branches"}


def test_running_cost_rates_are_blank_until_set_audited_and_validated(admin_client, db):
    page = admin_client.get("/settings").text
    assert "set your plan's rate" in page and 'id="running-costs"' in page
    assert settings.running_cost_rates(db) == {f["key"]: None for f in settings.rate_fields()}
    r = admin_client.post("/settings/running-costs", data={"neon.compute_unit_seconds": "abc"})
    assert "Nothing was saved" in flash_of(r.headers["location"])["err"]
    r = admin_client.post("/settings/running-costs", data={"neon.compute_unit_seconds": "0.106", "neon.root_branch_bytes_month": ""})
    assert "Saved 1 rate" in flash_of(r.headers["location"])["ok"]
    with session() as fresh:
        assert settings.running_cost_rates(fresh)["neon.compute_unit_seconds"] == Decimal("0.106")
        [row] = fresh.query(AuditLog).filter(AuditLog.action == "settings.running_cost_rates").all()
        assert row.after == {"neon.compute_unit_seconds": "0.106"} and row.before == {"neon.compute_unit_seconds": None}
    r = admin_client.post("/settings/running-costs", data={"neon.compute_unit_seconds": ""})
    with session() as fresh:
        assert settings.running_cost_rates(fresh)["neon.compute_unit_seconds"] is None, "blank clears a rate"


# ---- CSV import --------------------------------------------------------------------------------------------------
def test_a_linkedin_export_with_title_lines_and_us_dates_becomes_spend_rows():
    text_in = (FIX / "linkedin_campaigns.csv").read_text()
    with pytest.raises(csv_import.CSVProblem, match="choose the file's date format"):
        csv_import.parse(text_in, platform="linkedin")
    result = csv_import.parse(text_in, platform="linkedin", date_format="mdy")
    assert [(r.date, r.campaign_id, r.spend_pence, r.product, r.clicks) for r in result.rows] == [
        (D20, "6100001", 123450, "pp", 120), (D21, "6100001", 2000, "pp", 8), (D21, "6100002", 500, "group", 1)]
    assert result.rows[0].account_id == "508000001" and result.rows[0].raw["via"] == "csv"
    assert result.skipped == 1, "the totals line"


def test_a_meta_export_uses_the_form_s_account_and_names_stand_in_for_ids():
    result = csv_import.parse((FIX / "meta_export.csv").read_text(), platform="meta", account_id="act_123")
    assert [(r.date, r.campaign_id, r.account_id, r.spend_pence, r.product) for r in result.rows] == [
        (D20, "name:kp-launch-202611-brief", "act_123", 1050, "kp"), (D21, "name:kp-launch-202611-brief", "act_123", 950, "kp")]
    with pytest.raises(csv_import.CSVProblem, match="row 2"):
        csv_import.parse("Day,Campaign name,Spend\n2026-10-20,x,twelve\n", platform="meta")


def test_the_csv_import_route_dry_runs_then_writes_and_audits(admin_client):
    files = {"file": ("meta.csv", (FIX / "meta_export.csv").read_bytes(), "text/csv")}
    r = admin_client.post("/growth/import", data={"platform": "meta", "account_id": "act_123", "dry_run": "1"}, files=files)
    assert r.status_code == 200 and "nothing was written" in r.text and r.headers["cache-control"] == "no-store"
    with session() as fresh:
        assert fresh.query(AdSpend).count() == 0
    files = {"file": ("meta.csv", (FIX / "meta_export.csv").read_bytes(), "text/csv")}
    r = admin_client.post("/growth/import", data={"platform": "meta", "account_id": "act_123"}, files=files)
    assert r.status_code == 303 and "Imported 2 meta campaign days" in flash_of(r.headers["location"])["ok"]
    files = {"file": ("meta.csv", (FIX / "meta_export.csv").read_bytes(), "text/csv")}
    admin_client.post("/growth/import", data={"platform": "meta", "account_id": "act_123"}, files=files)
    with session() as fresh:
        assert fresh.query(AdSpend).count() == 2, "importing the same days again replaces them"
        actions = [a.after.get("dry_run") for a in fresh.query(AuditLog).filter(AuditLog.action == "growth.csv_import").all()]
        assert actions == [True, None, None]


# ---- the sales attribution view --------------------------------------------------------------------------------
def test_sales_attribution_precedence_promo_then_utm_then_affiliate_then_first_touch_then_direct(db):
    from app.services.growth.collectors.base import PurchaseRow

    def purchase(ref, **kw):
        return PurchaseRow(product=kw.pop("product", "pp"), purchase_ref=ref, occurred_at=datetime(2026, 10, 20, 12),
                           kind="subscription", amount_pence=1900, **kw)

    google = {"utm_source": "google", "utm_medium": "cpc", "utm_campaign": "pp-launch-202611-search"}
    cases = {
        "promo": purchase("r1", promo_code="ppmeta", last_touch=google),
        "unmapped": purchase("r2", promo_code="PPX", last_touch=google),
        "affiliate": purchase("r3", affiliate_ref="aff-1", first_touch={"utm_source": "brevo", "utm_medium": "email"}),
        "first": purchase("r4", first_touch={"utm_source": "Facebook", "utm_medium": "paid_social", "utm_campaign": "pp-a"}),
        "direct": purchase("r5"),
        "partner": purchase("r6", last_touch={"utm_source": "kpuk", "utm_medium": "referral"}),
        "other": purchase("r7", last_touch={"utm_source": "newsletter-x"}),
        "social": purchase("r8", last_touch={"utm_source": "zernio", "utm_medium": "social"}),
        "other_product": purchase("r9", product="kp", promo_code="PPMETA"),
        "utm_beats_affiliate": purchase("r10", affiliate_ref="aff-2", last_touch=google,
                                        first_touch={"utm_source": "brevo", "utm_medium": "email"}),
    }
    store.upsert_purchases(db, cases.values())
    db.add(PromoChannelMap(code="PPMETA", product="pp", channel="paid_social", platform="meta",
                           campaign_name="pp-launch-202611-hmo", created_by=ADMIN_EMAIL))
    db.commit()
    view = {row.purchase_ref: row for row in db.execute(text("SELECT * FROM sales_attribution")).mappings()}
    expected = {
        "r1": ("promo", "paid_social", "meta", "pp-launch-202611-hmo"),
        "r2": ("last_touch", "paid_search", "google", "pp-launch-202611-search"),
        "r3": ("affiliate", "affiliate", "", ""),
        "r4": ("first_touch", "paid_social", "meta", "pp-a"),
        "r5": ("direct", "direct", "", ""),
        "r6": ("last_touch", "partner", "kpuk", ""),
        "r7": ("last_touch", "other", "newsletter-x", ""),
        "r8": ("last_touch", "social", "zernio", ""),
        "r9": ("direct", "direct", "", ""),
        "r10": ("last_touch", "paid_search", "google", "pp-launch-202611-search"),
    }
    mapping = {"PPMETA": {"channel": "paid_social", "platform": "meta", "campaign_name": "pp-launch-202611-hmo"}}
    for row in cases.values():
        got = view[row.purchase_ref]
        assert (got["attribution_basis"], got["channel"], got["platform"], got["campaign_name"]) == expected[row.purchase_ref], row.purchase_ref
        twin = attribution.resolve(promo_code=row.promo_code.upper(), affiliate_ref=row.affiliate_ref,
                                   promo_map=mapping.get(row.promo_code.upper()) if row.product == "pp" else None,
                                   last_touch=row.last_touch, first_touch=row.first_touch)
        assert tuple(twin.values()) == expected[row.purchase_ref], f"the Python twin disagrees on {row.purchase_ref}"


# ---- the job --------------------------------------------------------------------------------------------------
def test_the_job_refuses_a_database_its_build_does_not_match(monkeypatch):
    monkeypatch.setattr(job.migrate, "current_revision", lambda: "0006_admin_switches")
    out = io.StringIO()
    with respx.mock(assert_all_called=False) as mock:
        assert job.main(["--tier", "all"], out=out) == 1
        assert mock.calls.call_count == 0
    assert "growth_pull refused" in out.getvalue() and migrate.head_revision() in out.getvalue()
    with session() as fresh:
        assert fresh.query(MetricRun).count() == 0 and fresh.query(AuditLog).count() == 0


def test_a_source_switched_off_is_skipped_silently(creds, live):
    out = io.StringIO()
    with respx.mock(assert_all_called=False) as mock:
        assert job.main(["--tier", "all"], out=out) == 0
        assert mock.calls.call_count == 0
    assert "0 sources run" in out.getvalue() and "skipped" not in out.getvalue()


def test_a_failed_source_fails_the_run_and_keeps_no_secret(creds, live):
    switch_on("product_pp")
    out = io.StringIO()
    with respx.mock(base_url=PP) as mock:
        mock.get("/api/partner/grouphq/metrics").mock(return_value=httpx.Response(500, json={"detail": "boom test-pp_service_token"}))
        assert job.main(["--tier", "hourly"], out=out) == 1
    assert "failed product_pp" in out.getvalue() and "test-pp_service_token" not in out.getvalue()
    with session() as fresh:
        [run] = fresh.query(MetricRun).all()
        assert not run.ok and run.actor == "job:growth" and run.tier == "hourly" and "test-pp_service_token" not in run.error
        health = fresh.get(MetricSource, "product_pp")
        assert health.status == "failing" and health.last_error and health.last_ok_at is None
        assert fresh.query(MetricSnapshot).count() == 0


def test_an_enabled_source_not_configured_here_fails_the_run_and_names_the_variable(creds, live, monkeypatch):
    switch_on("brevo")
    monkeypatch.setattr(config, "BREVO_API_KEY_HQ", "")
    out = io.StringIO()
    with respx.mock(assert_all_called=False) as mock:
        assert job.main(["--tier", "daily"], out=out) == 1
        assert mock.calls.call_count == 0
    assert "brevo is On but this job has no BREVO_API_KEY_HQ" in out.getvalue()
    with session() as fresh:
        assert fresh.get(MetricSource, "brevo").status == "not_configured"


def test_a_good_run_writes_once_and_a_second_run_leaves_the_same_rows(creds, live):
    switch_on("product_pp", "stripe_mh196")
    counts = []
    for _ in range(2):
        out = io.StringIO()
        with respx.mock(assert_all_called=False) as mock:
            mock.get(f"{PP}/api/partner/grouphq/metrics").mock(return_value=ok("product_pp_metrics.json"))
            mock.get(f"{PP}/api/partner/grouphq/purchases").mock(
                side_effect=[ok("product_pp_purchases_1.json"), ok("product_pp_purchases_2.json")])
            mock.get(f"{STRIPE}/v1/subscriptions").mock(side_effect=[ok("stripe_subscriptions_1.json"), ok("stripe_subscriptions_2.json")])
            mock.get(f"{STRIPE}/v1/checkout/sessions").mock(return_value=ok("stripe_checkout_sessions.json"))
            mock.get(f"{STRIPE}/v1/balance_transactions").mock(return_value=ok("stripe_balance_transactions.json"))
            mock.get(f"{STRIPE}/v1/promotion_codes").mock(return_value=ok("stripe_promotion_codes.json"))
            mock.get(f"{STRIPE}/v1/coupons/ORD1").mock(return_value=ok("stripe_coupon_ORD1.json"))
            assert job.main(["--tier", "hourly"], out=out) == 0, out.getvalue()
        assert "ok product_pp 26 rows" in out.getvalue() and "ok stripe_mh196" in out.getvalue()
        with session() as fresh:
            counts.append((fresh.query(MetricSnapshot).count(), fresh.query(ProductPurchase).count(),
                           fresh.query(func_sum(MetricSnapshot.value)).scalar()))
    assert counts[0] == counts[1], "collecting twice leaves the same rows"
    with session() as fresh:
        assert fresh.get(MetricSource, "product_pp").status == "ok"
        [audit_row, _] = fresh.query(AuditLog).filter(AuditLog.action == "growth.pull").all()
        assert audit_row.actor == "job:growth" and audit_row.after["sources"]["product_pp"]["state"] == "ok"


def func_sum(column):
    from sqlalchemy import func
    return func.sum(column)


def test_a_dry_run_of_every_source_prints_redacted_plans_and_writes_nothing(creds):
    switch_on(*COLLECTORS)
    out = io.StringIO()
    with respx.mock(assert_all_called=False) as mock:
        assert job.main(["--tier", "all", "--dry-run"], out=out) == 0, out.getvalue()
        assert mock.calls.call_count == 0
    printed = out.getvalue()
    assert printed.count("dry-run ") == len(COLLECTORS) and "plan: " in printed
    with session() as fresh:
        assert fresh.query(MetricSnapshot).count() == 0 and fresh.query(ProductPurchase).count() == 0
        runs = fresh.query(MetricRun).all()
        assert len(runs) == len(COLLECTORS) and all(r.dry_run and r.plan for r in runs)
        stored = json.dumps([r.plan for r in runs]) + json.dumps([a.after for a in fresh.query(AuditLog).all()])
    for secret in every_secret():
        assert secret not in printed and secret not in stored


def test_since_is_refused_on_the_hourly_tier():
    with pytest.raises(SystemExit):
        job.main(["--tier", "hourly", "--since", "2026-10-01"], out=io.StringIO())


# ---- the screens and the buttons --------------------------------------------------------------------------------
SCREENS = ("/growth", "/growth/channels", "/growth/email", "/growth/social", "/growth/search", "/growth/costs", "/growth/sources")


def test_the_screens_need_a_login(client):
    for path in SCREENS:
        r = client.get(path)
        assert r.status_code == 303 and r.headers["location"] == f"/login?next={path}"
    assert client.post("/growth/sources/brevo/switch", data={"mode": "on"}).status_code == 303


def seed_figures(db) -> date:
    today = reports.today_london()
    rows = {
        "ga4": [SnapshotRow(today, "pp", "visitors", 340), SnapshotRow(today, "pp", "sign_ups_pixel", 5)],
        "product_pp": [SnapshotRow(today, "pp", "sign_ups", 17), SnapshotRow(today, "pp", "paying_active", 31),
                       SnapshotRow(today, "pp", "mrr_pence", 68900), SnapshotRow(today, "pp", "purchases", 2),
                       SnapshotRow(today, "pp", "promo_redemptions", 1, "promo:PPMETA")],
        "stripe_mh196": [SnapshotRow(today, "pp", "mrr_pence", 70000)],
        "brevo": [SnapshotRow(today, "kp", "email_contacts", 412, "list:KPUK Investors"),
                  SnapshotRow(today, "kp", "email_sent", 400, "campaign:kp-launch-202611-brief"),
                  SnapshotRow(today, "kp", "email_delivered", 396, "campaign:kp-launch-202611-brief"),
                  SnapshotRow(today, "kp", "email_opens_unique", 180, "campaign:kp-launch-202611-brief")],
        "zernio": [SnapshotRow(today, "pp", "post_impressions", 1200),
                   SnapshotRow(today, "pp", "followers", 1012, "account:instagram:propertypredator"),
                   SnapshotRow(today, "pp", "post_engagements", 37, "post:zernio:p1", {"url": "https://www.instagram.com/p/abc/", "platform": "instagram"})],
        "search_console": [SnapshotRow(today - timedelta(days=3), "pp", "search_clicks", 16),
                           SnapshotRow(today - timedelta(days=3), "pp", "search_impressions", 600),
                           SnapshotRow(today - timedelta(days=3), "pp", "search_clicks", 4, "query:hmo calculator")],
        "neon": [SnapshotRow(today, "group", "neon_compute_unit_seconds", 7200, "project:proj-hq-222",
                             {"label": "grouphq", "project_name": "group-hq", "project_id": "proj-hq-222"})],
    }
    for source, values in rows.items():
        store.upsert_snapshots(db, source, values)
    db.add(AdSpend(date=today, platform="meta", account_id="act_1", campaign_id="c1", campaign_name="pp-launch-202611-hmo",
                   product="pp", spend_pence=5000, impressions=10000, clicks=100, conversions_purchase=Decimal(1)))
    db.add(PromoChannelMap(code="PPMETA", product="pp", channel="paid_social", platform="meta",
                           campaign_name="pp-launch-202611-hmo", created_by=ADMIN_EMAIL))
    db.add(ProductPurchase(product="pp", purchase_ref="cs_1", occurred_at=datetime.combine(today, datetime.min.time()),
                           kind="subscription", amount_pence=1900, promo_code="PPMETA"))
    db.add(ProductPurchase(product="pp", purchase_ref="cs_2", occurred_at=datetime.combine(today, datetime.min.time()),
                           kind="one_off", amount_pence=3900, promo_code="PPNEW"))
    db.commit()
    return today


def test_every_screen_renders_the_seeded_figures(admin_client, db):
    seed_figures(db)
    seed.ensure_metric_sources(db)
    db.commit()
    pages = {path: admin_client.get(path + "?period=7d&product=all") for path in SCREENS}
    for path, r in pages.items():
        assert r.status_code == 200, path
        assert 'href="/growth" class="is-active"' in r.text, f"{path}: the Growth nav line is active"
    overview = pages["/growth"].text
    assert "340" in overview and "17" in overview and "£689" in overview and "Stripe says £700" in overview
    assert "£50" in overview and "pixel saw 5" in overview and "<svg class=\"sparkline\"" in overview
    assert "Group HQ" in overview and "set your plan's rates" in overview, "running costs, unpriced"
    channels = pages["/growth/channels"].text
    assert "pp-launch-202611-hmo" in channels and "PPNEW" in channels, "an unmapped promo code is flagged"
    assert "KPUK Investors" in pages["/growth/email"].text and "45.5%" in pages["/growth/email"].text
    assert "propertypredator" in pages["/growth/social"].text and "https://www.instagram.com/p/abc/" in pages["/growth/social"].text
    assert "hmo calculator" in pages["/growth/search"].text and "2.7%" in pages["/growth/search"].text
    assert "Group HQ" in pages["/growth/costs"].text and "2.0000 CU-hour" in pages["/growth/costs"].text
    sources = pages["/growth/sources"].text
    assert "awaiting LinkedIn Advertising API approval" in sources and "NEON_API_KEY" in sources
    assert admin_client.get("/growth?period=today&product=kp").status_code == 200
    assert admin_client.get("/growth?period=nonsense&product=nonsense").status_code == 200, "unknown filters fall back"


def test_the_screens_render_with_nothing_configured_and_nothing_pulled(admin_client):
    for path in SCREENS:
        r = admin_client.get(path)
        assert r.status_code == 200, path
    assert "Every source is off" in admin_client.get("/growth").text
    assert admin_client.get("/growth/sources").text.count("Not configured") + admin_client.get("/growth/sources").text.count("Off") > 5


def test_the_switch_is_stored_audited_and_says_when_the_source_is_not_configured(admin_client, monkeypatch):
    monkeypatch.setattr(config, "NEON_API_KEY", "")
    r = admin_client.post("/growth/sources/neon/switch", data={"mode": "on"})
    assert "NEON_API_KEY" in flash_of(r.headers["location"])["err"]
    r = admin_client.post("/growth/sources/neon/switch", data={"mode": "on"})
    assert "Nothing changed" in flash_of(r.headers["location"])["ok"]
    r = admin_client.post("/growth/sources/neon/switch", data={"mode": "off"})
    assert "is off" in flash_of(r.headers["location"])["ok"]
    assert "not built yet" in flash_of(admin_client.post("/growth/sources/linkedin/switch", data={"mode": "on"}).headers["location"])["err"]
    assert admin_client.post("/growth/sources/nowhere/switch", data={"mode": "on"}).status_code == 404
    with session() as fresh:
        assert fresh.get(AppSetting, "growth_source.neon").value == "off"
        assert fresh.get(MetricSource, "neon").status == "off"
        rows = fresh.query(AuditLog).filter(AuditLog.action == "growth.source_switch").order_by(AuditLog.id).all()
        assert [(r.before["switch"], r.after["switch"]) for r in rows] == [("off", "on"), ("on", "off")]
        assert rows[0].entity == "source:neon" and rows[0].actor == ADMIN_EMAIL and rows[0].after["state"] == "not_configured"


def test_test_under_the_floor_shows_the_redacted_plan_and_audits(admin_client, creds):
    r = admin_client.post("/growth/sources/neon/test")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "Bearer [redacted]" in r.text and "console.neon.tech/api/v2/projects" in r.text
    for secret in every_secret():
        assert secret not in r.text
    with session() as fresh:
        [row] = fresh.query(AuditLog).filter(AuditLog.action == "growth.source_test").all()
        assert row.entity == "source:neon" and row.after["ok"] is True and row.after["dry_run"] is True


def test_test_live_names_the_result_and_never_the_key(admin_client, creds, live):
    with respx.mock(base_url=BREVO) as mock:
        mock.get("/account").mock(return_value=ok("brevo_account.json"))
        r = admin_client.post("/growth/sources/brevo/test")
    assert r.status_code == 303 and "the test call answered (HTTP 200)" in flash_of(r.headers["location"])["ok"]
    with respx.mock(base_url=BREVO) as mock:
        mock.get("/account").mock(return_value=httpx.Response(401, json={"code": "unauthorized", "message": f"Key not found {SECRETS['BREVO_API_KEY_HQ']}"}))
        r = admin_client.post("/growth/sources/brevo/test")
    flash = flash_of(r.headers["location"])["err"]
    assert "unauthorized (HTTP 401)" in flash and SECRETS["BREVO_API_KEY_HQ"] not in flash
    with session() as fresh:
        rows = fresh.query(AuditLog).filter(AuditLog.action == "growth.source_test").order_by(AuditLog.id).all()
        assert [r.after["ok"] for r in rows] == [True, False]
        assert rows[0].after["detail"] == {"ok": True, "plan": ["free"]}, "the account's email is not kept"
        assert SECRETS["BREVO_API_KEY_HQ"] not in json.dumps([r.after for r in rows])


def test_pull_now_renders_its_run_page_and_records_the_run(admin_client, creds, live):
    with respx.mock(base_url=PP) as mock:
        mock.get("/api/partner/grouphq/metrics").mock(return_value=ok("product_pp_metrics.json"))
        mock.get("/api/partner/grouphq/purchases").mock(side_effect=[ok("product_pp_purchases_1.json"), ok("product_pp_purchases_2.json")])
        r = admin_client.post("/growth/sources/product_pp/pull")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store" and "26 rows written" in r.text
    assert "X-Service-Token" in r.text and "test-pp_service_token" not in r.text
    r = admin_client.post("/growth/sources/product_pp/pull", data={"dry_run": "1"})
    assert "nothing was fetched" in r.text
    with session() as fresh:
        runs = fresh.query(MetricRun).order_by(MetricRun.id).all()
        assert [(x.ok, x.dry_run, x.actor) for x in runs] == [(True, False, ADMIN_EMAIL), (True, True, ADMIN_EMAIL)]
        assert runs[0].plan is None and runs[1].plan, "plans are kept for dry runs only"
        assert [a.entity for a in fresh.query(AuditLog).filter(AuditLog.action == "growth.pull").all()] == ["source:product_pp"] * 2


def test_the_promo_map_is_edited_on_sources_with_an_audit_row_per_change(admin_client):
    r = admin_client.post("/growth/promo-map", data={"code": "ppmeta", "product": "pp", "channel": "paid_social",
                                                     "platform": "meta", "campaign_name": "pp-launch-202611-hmo"})
    assert "PPMETA" in flash_of(r.headers["location"])["ok"]
    r = admin_client.post("/growth/promo-map", data={"code": "PPMETA", "product": "pp", "channel": "paid_social",
                                                     "platform": "meta", "campaign_name": "pp-launch-202611-hmo"})
    assert "Nothing changed" in flash_of(r.headers["location"])["ok"]
    assert "err" in flash_of(admin_client.post("/growth/promo-map", data={"code": "bad code!", "product": "pp",
                                                                          "channel": "email"}).headers["location"])
    assert "err" in flash_of(admin_client.post("/growth/promo-map", data={"code": "X1", "product": "pp",
                                                                          "channel": "carrier_pigeon"}).headers["location"])
    r = admin_client.post("/growth/promo-map", data={"code": "PPMETA", "product": "pp", "action": "delete"})
    assert "no longer mapped" in flash_of(r.headers["location"])["ok"]
    with session() as fresh:
        assert fresh.query(PromoChannelMap).count() == 0
        rows = fresh.query(AuditLog).filter(AuditLog.action == "growth.promo_map").order_by(AuditLog.id).all()
        assert [(r.entity, r.before is None, r.after is None) for r in rows] == [
            ("promo:pp:PPMETA", True, False), ("promo:pp:PPMETA", False, True)]


def test_the_sources_page_lists_credentials_by_name_only(admin_client, creds):
    page = admin_client.get("/growth/sources").text
    for name in ("NEON_API_KEY", "STRIPE_RK_MH196", "GOOGLE_SA_JSON", "BREVO_API_KEY_HQ"):
        assert name in page
    for secret in every_secret():
        assert secret not in page
    mode = source_mode("neon", switches={})
    assert mode["state"] == "off" and mode["configured"] and mode["missing"] == []


def test_a_flash_redirect_keeps_its_fragment_after_the_query():
    from app.templating import go
    assert go("/settings#running-costs", ok="Saved").headers["location"] == "/settings?ok=Saved#running-costs"
    assert go("/growth/sources?x=1#promo", err="No").headers["location"] == "/growth/sources?x=1&err=No#promo"
    assert go("/settings").headers["location"] == "/settings"


def test_a_refused_row_rolls_back_its_source_but_the_run_is_still_recorded(creds, live, monkeypatch):
    def rows_for(self, projects, names, notes):
        return [SnapshotRow(D20, "group", "neon_compute_unit_seconds", 1, "project:a"), SnapshotRow(D20, "group", "not_a_metric", 1)]

    monkeypatch.setattr(NeonCollector, "rows_for", rows_for)
    with respx.mock(base_url=NEON) as mock:
        mock.get("/projects").mock(return_value=ok("neon_projects.json"))
        mock.get("/consumption_history/v2/projects").mock(return_value=ok("neon_consumption_3.json"))
        with session() as db:
            outcome = run_source(db, "neon", actor="job:growth")
    assert outcome.state == "failed" and outcome.reason == "store_refused" and "not_a_metric" in outcome.error
    with session() as fresh:
        assert fresh.query(MetricSnapshot).count() == 0, "one transaction per source: nothing half-written"
        [run] = fresh.query(MetricRun).all()
        assert not run.ok and fresh.get(MetricSource, "neon").status == "failing"


# ---- review fixes: keys that move, sums that must not be overwritten, isolation, the upload, the crons --------------
def _pull(db, source: str, routes: dict, *, tier: str | None = None, base_url: str | None = None):
    """run_source with the given {url or path: response or [responses]} answered by respx."""
    with respx.mock(base_url=base_url, assert_all_called=False) as mock:
        for url, answer in routes.items():
            route = mock.get(url) if not url.startswith("POST ") else mock.post(url[5:])
            route.mock(side_effect=answer) if isinstance(answer, list) else route.mock(return_value=answer)
        return run_source(db, source, tier=tier, actor="job:growth")


def test_relabelling_a_neon_project_keeps_it_one_line_counted_once(db, creds, live, monkeypatch):
    def pull():
        return _pull(db, "neon", {"/projects": ok("neon_projects.json"), "/consumption_history/v2/projects": [
            ok("neon_consumption_1.json"), ok("neon_consumption_2.json"), ok("neon_consumption_3.json")]}, base_url=NEON)
    monkeypatch.setattr(config, "NEON_PROJECT_PRODUCTS", "")                  # first: see the projects' names
    assert pull().state == "ok"
    today = date(2026, 10, 23)
    before = reports.running_costs(db, settings.running_cost_rates(db), today)
    assert "propertypredator-prod" in {m["label"] for m in before["month"]}
    monkeypatch.setattr(config, "NEON_PROJECT_PRODUCTS", "proj-pp-111=pp")    # then label them
    assert pull().state == "ok"
    after = reports.running_costs(db, settings.running_cost_rates(db), today)
    assert after["total"]["usage"] == before["total"]["usage"], "the same consumption re-pulled is counted once"
    labels = [m["label"] for m in after["month"]]
    assert "Property Predator" in labels and "propertypredator-prod" not in labels and len(labels) == len(set(labels))
    monkeypatch.setattr(config, "NEON_PROJECT_PRODUCTS", "")                  # a label removed: the name comes back
    assert "propertypredator-prod" in {m["label"] for m in reports.running_costs(db, {}, today)["month"]}


def test_a_brevo_list_remapped_to_another_product_leaves_no_stale_row(db, creds, live, monkeypatch):
    routes = {"/contacts/lists": ok("brevo_lists.json"), "/contacts": ok("brevo_contacts.json"),
              "/emailCampaigns": ok("brevo_campaigns.json"), "/smtp/statistics/reports": ok("brevo_smtp_reports.json")}
    assert _pull(db, "brevo", routes, base_url=BREVO).state == "ok"
    today = reports.today_london()
    contacts = lambda: {(r.product, r.dimension): r.value for r in db.query(MetricSnapshot).filter(  # noqa: E731
        MetricSnapshot.metric == "email_contacts", MetricSnapshot.date == today)}
    assert contacts()[("kp", "list:KPUK Investors")] == 412
    monkeypatch.setattr(config, "BREVO_LIST_PRODUCTS", "3=pp,7=pp")
    assert _pull(db, "brevo", routes, base_url=BREVO).state == "ok"
    db.expire_all()
    now = contacts()
    assert ("kp", "list:KPUK Investors") not in now and now[("pp", "list:KPUK Investors")] == 412
    assert not db.query(MetricSnapshot).filter(MetricSnapshot.metric == "email_contacts_new",
                                               MetricSnapshot.product == "kp").count(), "the new-contact days moved too"
    assert db.query(MetricSnapshot).filter(MetricSnapshot.dimension == "stream:transactional").count() > 0


def test_a_stripe_price_remapped_moves_its_subscription_and_counts_it_once(db, creds, live, monkeypatch):
    routes = {f"{STRIPE}/v1/subscriptions": [ok("stripe_subscriptions_1.json"), ok("stripe_subscriptions_2.json")],
              f"{STRIPE}/v1/checkout/sessions": ok("stripe_checkout_sessions.json"),
              f"{STRIPE}/v1/balance_transactions": ok("stripe_balance_transactions.json"),
              f"{STRIPE}/v1/promotion_codes": ok("stripe_promotion_codes.json"),
              f"{STRIPE}/v1/coupons/ORD1": ok("stripe_coupon_ORD1.json")}
    assert _pull(db, "stripe_mh196", dict(routes)).state == "ok"
    today = reports.today_london()
    paying = lambda: {r.product: r.value for r in db.query(MetricSnapshot).filter(  # noqa: E731
        MetricSnapshot.metric == "paying_active", MetricSnapshot.date == today)}
    first = paying()
    assert first["pp"] == 2
    monkeypatch.setattr(config, "STRIPE_PRODUCT_MAP", "")          # prod_pp no longer mapped: sub_2 falls to group
    routes[f"{STRIPE}/v1/subscriptions"] = [ok("stripe_subscriptions_1.json"), ok("stripe_subscriptions_2.json")]
    assert _pull(db, "stripe_mh196", routes).state == "ok"
    db.expire_all()
    second = paying()
    assert second["pp"] == 1 and sum(second.values()) == sum(first.values()), "moved, not counted twice"
    assert reports.latest(db, reports.STRIPE, "paying_active", today, reports.PRODUCT_ORDER + ("group",)) == second
    store.upsert_snapshots(db, "stripe_hnh", [SnapshotRow(today - timedelta(days=1), "kp", "paying_active", 7),
                                              SnapshotRow(today, "hnh", "paying_active", 2)])
    db.commit()
    levels = reports.latest(db, reports.STRIPE, "paying_active", today, reports.PRODUCT_ORDER + ("group",))
    assert levels.get("kp") == second.get("kp") and levels["hnh"] == 2, \
        "kp's 7 from Hello New Home's account is not on that source's latest day: no stale level is added"
    assert reports.latest(db, ("stripe_hnh",), "paying_active", today - timedelta(days=1), ("kp",)) == {"kp": Decimal(7)}


def test_a_product_day_restated_by_a_shifted_window_replaces_only_what_it_answers(db, creds, live):
    first = fx("product_pp_metrics.json")
    second = {"ok": True, "product": "pp", "days": [{"date": "2026-10-21", "sign_ups": 9, "promo_redemptions": {}}]}
    empty = {"ok": True, "purchases": [], "next_cursor": None}
    path = "/api/partner/grouphq/metrics"
    assert _pull(db, "product_pp", {path: httpx.Response(200, json=first),
                                    "/api/partner/grouphq/purchases": httpx.Response(200, json=empty)}, base_url=PP).state == "ok"
    assert _pull(db, "product_pp", {path: httpx.Response(200, json=second),
                                    "/api/partner/grouphq/purchases": httpx.Response(200, json=empty)}, base_url=PP).state == "ok"
    db.expire_all()
    rows = {(r.date, r.metric, r.dimension): r.value for r in db.query(MetricSnapshot).all()}
    assert rows[(D21, "sign_ups", "")] == 9, "the restated day takes the newest answer"
    assert (D21, "promo_redemptions", "promo:PPGOOGLE") not in rows, "a code the day no longer reports is gone"
    assert rows[(D21, "mrr_pence", "")] == 70800, "a metric the day omits keeps its earlier value"
    assert rows[(D20, "sign_ups", "")] == 12 and rows[(D20, "promo_redemptions", "promo:PPMETA")] == 1, "outside: untouched"


def test_a_product_s_codes_in_two_cases_add_up_and_a_day_listed_twice_is_refused(db, creds, live):
    body = {"ok": True, "product": "pp", "days": [{"date": "2026-10-20", "promo_redemptions": {"ppmeta": 1, "PPMETA": 2}}]}
    rows = ProductPPCollector.from_config().metric_rows(body, [])
    assert keys(rows) == {(D20, "pp", "promo_redemptions", "promo:PPMETA"): 3}
    twice = {"ok": True, "product": "pp", "days": [{"date": "2026-10-20", "sign_ups": 3}, {"date": "2026-10-20", "sign_ups": 4}]}
    outcome = _pull(db, "product_pp", {"/api/partner/grouphq/metrics": httpx.Response(200, json=twice),
                                       "/api/partner/grouphq/purchases": httpx.Response(200, json={"ok": True, "purchases": []})},
                    base_url=PP)
    assert outcome.state == "failed" and outcome.reason == "store_refused" and "two different values" in outcome.error


def test_a_purchase_refunded_since_the_last_pull_is_updated(db, creds, live):
    bought = fx("product_pp_purchases_2.json")
    metrics = {"ok": True, "product": "pp", "days": []}
    for status in ("paid", "refunded"):
        bought["purchases"][0]["status"] = status
        assert _pull(db, "product_pp", {"/api/partner/grouphq/metrics": httpx.Response(200, json=metrics),
                                        "/api/partner/grouphq/purchases": httpx.Response(200, json=bought)},
                     base_url=PP).state == "ok"
    db.expire_all()
    [row] = db.query(ProductPurchase).filter(ProductPurchase.purchase_ref == bought["purchases"][0]["ref"]).all()
    assert row.status == "refunded"


def test_csv_rows_for_one_campaign_day_are_added_up_and_a_reimport_restates_spend(db):
    text_in = ("Day,Campaign name,Ad set name,Amount spent (GBP),Impressions\n"
               "2026-10-20,pp-launch-202611-hmo,set A,10.00,100\n"
               "2026-10-20,pp-launch-202611-hmo,set B,15.00,50\n"
               "2026-10-21,pp-launch-202611-hmo,set A,5.00,10\n")
    parsed = csv_import.parse(text_in, platform="meta", account_id="act_1")
    assert [(r.date, r.spend_pence, r.impressions, r.raw["rows_summed"]) for r in parsed.rows] == [
        (D20, 2500, 150, 2), (D21, 500, 10, 1)]
    assert parsed.rows[0].raw["lines"] == [2, 3] and "Ad set name" not in parsed.rows[0].raw
    store.upsert_spend(db, parsed.rows)
    db.commit()
    store.upsert_spend(db, csv_import.parse(text_in.replace("15.00", "25.00"), platform="meta", account_id="act_1").rows)
    db.commit()
    db.expire_all()
    assert {r.date: r.spend_pence for r in db.query(AdSpend).all()} == {D20: 3500, D21: 500}, "the newest export wins"
    with pytest.raises(ValueError, match="added up"):
        store.spend_values([parsed.rows[0], dataclass_replace(parsed.rows[0], spend_pence=1)])


def dataclass_replace(row, **changes):
    import dataclasses
    return dataclasses.replace(row, **changes)


def test_clerk_fills_ordris_sign_ups_only_on_days_the_ledger_is_silent(db):
    today = reports.today_london()
    yesterday = today - timedelta(days=1)
    store.upsert_snapshots(db, "clerk_ordris", [SnapshotRow(today, "ordris", "sign_ups", 4, meta={"via": "clerk"}),
                                                SnapshotRow(yesterday, "ordris", "sign_ups", 6, meta={"via": "clerk"})])
    db.commit()
    data = reports.overview(db, reports.period_of("7d"), "ordris", {}, {})
    assert data["tiles"]["sign_ups"]["value"] == "10" and data["table"][0]["sign_ups"] == "10"
    assert "Ordris from Clerk on 2 days" in data["tiles"]["sign_ups"]["note"]
    store.upsert_snapshots(db, "product_ordris", [SnapshotRow(yesterday, "ordris", "sign_ups", 5)])
    db.commit()
    data = reports.overview(db, reports.period_of("7d"), "all", {}, {})
    assert data["tiles"]["sign_ups"]["value"] == "9", "the ledger's 5 wins yesterday, Clerk's 4 fills today: never 15"
    assert data["table"][2]["product"] == "ordris" and data["table"][2]["spark"][-2:] == [5.0, 4.0]


def test_an_hourly_realtime_read_never_hides_a_failed_daily_ga4_pull(creds, live):
    with session() as db:
        daily = _pull(db, "ga4", {f"POST {TOKEN_URI}": ok("google_token.json"),
                                  f"POST {GA4}/properties/111111111:batchRunReports": httpx.Response(
                                      429, json={"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota"}})}, tier="daily")
    with session() as db:
        hourly = _pull(db, "ga4", {f"POST {TOKEN_URI}": ok("google_token.json"),
                                   f"POST {GA4}/properties/111111111:runRealtimeReport": ok("ga4_realtime.json")},
                       tier="hourly")
    assert daily.state == "failed" and hourly.state == "ok"
    with session() as db:
        health = db.get(MetricSource, "ga4")
        assert health.status == "failing" and "RESOURCE_EXHAUSTED" in health.last_error and health.last_ok_at is None
        assert reports._as_of(reports.health(db), "ga4") is None, "visitors are not as of the realtime read"
        assert [(r.tier, r.ok) for r in db.query(MetricRun).order_by(MetricRun.id)] == [("daily", False), ("hourly", True)]


def test_channels_count_paid_purchases_on_their_london_day(db):
    today = reports.today_london()                                  # BST until 25 October 2026
    midnight = datetime.combine(today, datetime.min.time())
    db.add(PromoChannelMap(code="PPMETA", product="pp", channel="paid_social", platform="meta", campaign_name="pp-x",
                           created_by=ADMIN_EMAIL))
    for ref, at, status in (("late", midnight - timedelta(minutes=30), "paid"),       # 00:30 London today
                            ("refunded", midnight + timedelta(hours=12), "refunded"),
                            ("trial", midnight + timedelta(hours=12), "trialing"),
                            ("yesterday", midnight - timedelta(hours=2), "paid")):    # 23:00 London yesterday
        db.add(ProductPurchase(product="pp", purchase_ref=ref, occurred_at=at, kind="subscription", amount_pence=1900,
                               status=status, promo_code="PPMETA"))
    db.commit()
    data = reports.channels(db, reports.period_of("today"), "pp")
    [row] = [r for r in data["rows"] if r["campaign"] == "pp-x"]
    assert (row["money_purchases"], row["revenue"]) == (1, 1900), "one paid sale today; refunded and trialing are not sales"
    assert reports.promo_purchases(db, today, today, ("pp",)) == {"pp": 1}, "Channels and the Overview agree"


def test_one_source_s_nul_error_body_never_stops_the_others(creds, live):
    switch_on("stripe_mh196", "product_pp")
    out = io.StringIO()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith=STRIPE).mock(
            return_value=httpx.Response(502, content=b"\x00\x1f\x8b upstream", headers={"content-type": "text/plain"}))
        pp_metrics = mock.get(f"{PP}/api/partner/grouphq/metrics").mock(return_value=ok("product_pp_metrics.json"))
        mock.get(f"{PP}/api/partner/grouphq/purchases").mock(return_value=ok("product_pp_purchases_2.json"))
        assert job.main(["--tier", "hourly"], out=out) == 1
    assert pp_metrics.called and "failed stripe_mh196" in out.getvalue() and "ok product_pp" in out.getvalue()
    with session() as fresh:
        assert {(r.source, r.ok) for r in fresh.query(MetricRun)} == {("stripe_mh196", False), ("product_pp", True)}
        health = fresh.get(MetricSource, "stripe_mh196")
        assert health.status == "failing" and "\x00" not in health.last_error and "http_502" in health.last_error
        assert fresh.query(AuditLog).filter(AuditLog.action == "growth.pull").count() == 1
    assert scrub("a\x00b\x1fc\nd") == "a b c\nd"


def test_a_run_whose_record_cannot_be_written_is_still_isolated(creds, live, monkeypatch):
    from app.services.growth import runner
    switch_on("product_pp", "stripe_mh196")
    calls = {"n": 0}
    real = runner._record

    def flaky(db, source, *args, **kwargs):
        calls["n"] += 1
        if source == "product_pp" and kwargs.get("plans", True):
            db.execute(text("SELECT 1/0"))                          # a database error while recording
        return real(db, source, *args, **kwargs)

    monkeypatch.setattr(runner, "_record", flaky)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith=STRIPE).mock(return_value=httpx.Response(200, json={"object": "list", "data": [], "has_more": False}))
        mock.get(f"{PP}/api/partner/grouphq/metrics").mock(return_value=ok("product_pp_metrics.json"))
        mock.get(f"{PP}/api/partner/grouphq/purchases").mock(return_value=ok("product_pp_purchases_2.json"))
        out = io.StringIO()
        assert job.main(["--tier", "hourly"], out=out) == 1
    assert "failed product_pp the run could not be recorded (DataError)" in out.getvalue() and "ok stripe_mh196" in out.getvalue()
    with session() as fresh:
        assert fresh.query(MetricSnapshot).filter(MetricSnapshot.source == "product_pp").count() == 0, "its rows rolled back"
        assert fresh.get(MetricSource, "product_pp").status == "failing"


def test_test_with_a_key_that_is_not_an_rsa_key_answers_with_an_error_not_a_500(admin_client, creds, live, monkeypatch):
    bad = dict(service_account())
    pem = "PRIVATE " + "KEY-----"                                   # built here so no scanner reads a key block
    bad["private_key"] = f"-----BEGIN {pem}\nU0VDUkVUS0VZTUFURVJJQUxfQUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n-----END {pem}\n"
    monkeypatch.setattr(config, "GOOGLE_SA_JSON", json.dumps(bad))
    with respx.mock(assert_all_called=False) as mock:
        for source in ("ga4", "search_console"):
            r = admin_client.post(f"/growth/sources/{source}/test")
            assert r.status_code == 303 and "bad_credentials" in flash_of(r.headers["location"])["err"]
        assert mock.calls.call_count == 0
    with session() as fresh:
        rows = fresh.query(AuditLog).filter(AuditLog.action == "growth.source_test").all()
        assert [r.after["ok"] for r in rows] == [False, False]
        assert "U0VDUkVU" not in json.dumps([r.after for r in rows])


def test_the_ga4_test_names_a_property_not_on_london_time(admin_client, creds, live):
    for zone, verdict in (("Europe/London", "ok"), ("America/Los_Angeles", "err")):
        with respx.mock() as mock:
            mock.post(TOKEN_URI).mock(return_value=ok("google_token.json"))
            mock.get("https://analyticsadmin.googleapis.com/v1beta/accountSummaries").mock(return_value=ok("ga4_account_summaries.json"))
            mock.get("https://analyticsadmin.googleapis.com/v1beta/properties/111111111").mock(
                return_value=httpx.Response(200, json=fx("ga4_property_pp.json") | {"timeZone": zone}))
            r = admin_client.post("/growth/sources/ga4/test")
        flash = flash_of(r.headers["location"])
        assert verdict in flash, flash
        if verdict == "err":
            assert "America/Los_Angeles" in flash["err"] and "the test call answered (HTTP 200)" in flash["err"]


def test_an_anonymous_upload_is_not_read_before_the_login_check(client, monkeypatch):
    import starlette.formparsers as formparsers
    seen = {"bytes": 0}
    original = formparsers.MultiPartParser.on_part_data

    def counting(self, data, start, end):
        seen["bytes"] += end - start
        return original(self, data, start, end)

    monkeypatch.setattr(formparsers.MultiPartParser, "on_part_data", counting)
    r = client.post("/growth/import", files={"file": ("big.csv", b"a" * (5 * 1024 * 1024), "text/csv")}, data={"platform": "meta"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login") and seen["bytes"] == 0


def test_an_upload_over_the_limit_is_refused_by_its_size_before_it_is_parsed(admin_client, monkeypatch):
    import starlette.formparsers as formparsers
    seen = {"bytes": 0}
    original = formparsers.MultiPartParser.on_part_data
    monkeypatch.setattr(formparsers.MultiPartParser, "on_part_data",
                        lambda self, data, start, end: (seen.__setitem__("bytes", seen["bytes"] + end - start),
                                                        original(self, data, start, end))[1])
    r = admin_client.post("/growth/import", files={"file": ("big.csv", b"a" * (3 * 1024 * 1024), "text/csv")},
                          data={"platform": "meta"})
    assert "over 2 MB" in flash_of(r.headers["location"])["err"] and seen["bytes"] == 0
    with session() as fresh:
        assert fresh.query(AuditLog).filter(AuditLog.action == "growth.csv_import").count() == 0


def test_the_cron_column_keeps_the_daily_run_s_answer_and_idle_runs_write_nothing(creds, live, monkeypatch):
    switch_on("neon")
    monkeypatch.setattr(config, "NEON_API_KEY", "")                 # the daily cron lacks the key
    with respx.mock(assert_all_called=False):
        assert job.main(["--tier", "daily"], out=io.StringIO()) == 1
    for _ in range(25):                                             # a day of hourly runs with nothing hourly on
        with respx.mock(assert_all_called=False):
            assert job.main(["--tier", "hourly"], out=io.StringIO()) == 0
    with session() as db:
        assert db.query(AuditLog).filter(AuditLog.action == "growth.pull").count() == 1, "an idle run writes no audit row"
        [neon] = [r for r in reports.sources(db, settings.growth_source_switches(db)) if r["source"] == "neon"]
        assert neon["cron"] is False, "the daily cron's missing variable still shows"
    switch_on("product_pp")
    with respx.mock(base_url=PP, assert_all_called=False) as mock:
        mock.get("/api/partner/grouphq/metrics").mock(return_value=ok("product_pp_metrics.json"))
        mock.get("/api/partner/grouphq/purchases").mock(return_value=ok("product_pp_purchases_2.json"))
        for _ in range(6):
            job.main(["--tier", "hourly"], out=io.StringIO())
    with session() as db:
        view = reports.cron_view(db)
        assert view["neon"] is False and view["product_pp"] is True, "busy hourly runs do not push the daily one out"


def test_the_growth_crons_copy_only_what_the_web_service_holds():
    yaml = pytest.importorskip("yaml")
    services = {s["name"]: s for s in yaml.safe_load((Path(config.ROOT) / "render.yaml").read_text())["services"]}
    held = {e["key"] for e in services["group-hq-recompute"]["envVars"]}       # copied today, so held by the web service
    growth_vars = {"GOOGLE_SA_JSON", "GA4_PROPERTY_IDS", "GSC_SITES", "STRIPE_RK_MH196", "STRIPE_RK_HNH", "STRIPE_PRODUCT_MAP",
                   "BREVO_API_KEY_HQ", "BREVO_LIST_PRODUCTS", "ZERNIO_API_KEY", "ZERNIO_PROFILE_PRODUCTS",
                   "ZERNIO_AD_ACCOUNTS", "NEON_API_KEY", "NEON_ORG_ID", "NEON_PROJECT_PRODUCTS", "CLERK_SECRET_KEY_ORDRIS"}
    web = {e["key"]: e for e in services["group-hq"]["envVars"]}
    for name in growth_vars - {"CLERK_SECRET_KEY_ORDRIS"}:
        assert web[name].get("sync") is False, f"{name} is set in the dashboard, never in the file"
    assert "CLERK_SECRET_KEY_ORDRIS" not in web
    for cron in ("group-hq-growth-hourly", "group-hq-growth-daily"):
        copied = {e["key"] for e in services[cron]["envVars"]}
        assert all(e["fromService"]["envVarKey"] == e["key"] for e in services[cron]["envVars"])
        assert copied <= held, f"{cron} copies {copied - held}, which the web service may not hold"
        assert not copied & growth_vars, "a Growth variable's fromService line follows the variable, in a later commit"
    assert {"PP_SERVICE_TOKEN", "ORDRIS_PARTNER_TOKEN", "KP_PARTNER_TOKEN", "HNH_PARTNER_TOKEN"} <= {
        e["key"] for e in services["group-hq-growth-hourly"]["envVars"]}


def test_stripe_s_new_payers_never_include_one_that_has_not_paid():
    from app.services.growth.collectors.stripe_read import LEVELS, WINDOW_METRICS, paid_from
    now = int(datetime(2026, 10, 22, 12).timestamp())
    day = 86400
    monthly = {"items": {"data": [{"price": {"unit_amount": 1900, "currency": "gbp", "recurring": {"interval": "month"}}}]}}
    trial_end = now - 10 * day
    assert paid_from({**monthly, "status": "paused", "trial_end": trial_end}, now) is None, "a trial ended without a card"
    assert paid_from({**monthly, "status": "active", "trial_end": trial_end}, now) == trial_end
    assert paid_from({**monthly, "status": "past_due", "trial_end": trial_end}, now) == trial_end, "as Stripe counts it"
    failed_first = {**monthly, "status": "canceled", "trial_end": trial_end, "ended_at": trial_end + 14 * day,
                    "cancellation_details": {"reason": "payment_failed"}}
    assert paid_from(failed_first, now) is None, "cancelled for a failed payment inside the first month: never paid"
    assert paid_from({**failed_first, "ended_at": trial_end + 40 * day}, now + 40 * day) == trial_end, "paid once, then failed"
    assert paid_from({**failed_first, "cancellation_details": {"reason": "cancellation_requested"}}, now) == trial_end
    assert paid_from({**monthly, "status": "active", "trial_end": now + day}, now) is None, "still in its trial"
    registered = {m.name for m in registry.METRICS if "stripe_mh196" in m.sources}
    assert set(LEVELS) | set(WINDOW_METRICS) == registered, "the replace windows cover every Stripe metric"


def test_a_replace_window_is_checked_against_the_registry(db):
    with pytest.raises(ValueError, match="may not replace"):
        store.replace_rows(db, "brevo", [Replace(("visitors",), D20, D21)])
    store.upsert_snapshots(db, "brevo", [SnapshotRow(D20, "kp", "email_sent", 4, "campaign:a"),
                                         SnapshotRow(D20, "group", "email_sent", 9, "stream:transactional")])
    assert store.replace_rows(db, "brevo", [Replace(("email_sent",), D20, D20, "campaign:%")]) == 1
    db.commit()
    assert [(r.dimension, r.value) for r in db.query(MetricSnapshot).all()] == [("stream:transactional", Decimal("9.0000"))]


def test_a_nul_in_a_platform_value_is_stored_without_it(db):
    store.upsert_snapshots(db, "zernio", [SnapshotRow(D20, "pp", "post_engagements", 3, "post:zernio:p\x001",
                                                      {"url": "https://x.example/\x00p", "platform": "x"})])
    from app.services.growth.collectors.base import SpendRow
    store.upsert_spend(db, [SpendRow(D20, "meta", "act\x00", "c\x001", "pp-\x00a", "pp", spend_pence=1, raw={"k\x00": "v\x00"})])
    db.commit()
    [row] = db.query(MetricSnapshot).all()
    assert row.dimension == "post:zernio:p1" and row.meta["url"] == "https://x.example/p"
    [spend] = db.query(AdSpend).all()
    assert (spend.account_id, spend.campaign_id, spend.campaign_name, spend.raw) == ("act", "c1", "pp-a", {"k": "v"})
