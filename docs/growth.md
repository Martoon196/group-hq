# Growth

One section of Group HQ fed by APIs: visitors, sign-ups, trials, paying, MRR, ad spend and cost per sale by product
and channel, plus email, social, search and the running costs of every database. Written 23 October 2026 for packet 1
of the tracking build (Group HQ side). Every figure on a Growth screen is read from Postgres; no page view ever calls a
platform. The collectors only read: nothing is ever sent to a platform from here (the products send their own
conversions).

| Screen | Path | Reads |
|---|---|---|
| Overview | `/growth` | tiles and a row per product, data freshness, running costs |
| Channels | `/growth/channels` | `ad_spend` beside `sales_attribution`: "pixel says" and "money says" (paid purchases, each on its London day) |
| Email | `/growth/email` | Brevo lists, campaigns, transactional health |
| Social | `/growth/social` | Zernio accounts, followers, top posts |
| Search | `/growth/search` | Search Console per site, top queries and pages, devices |
| Costs | `/growth/costs` | Neon usage per project, by day and month to date, priced from Settings |
| Sources | `/growth/sources` | the health page: switches, Test, Pull now, the promo code map, the CSV import |

Every screen takes `?period=today|7d|30d` and `?product=pp|kp|ordris|hnh|all`. Every route is behind `require_admin`
and every POST behind `check_origin`, as the rest of the app. The running-cost rates are on **Settings**.

## Tables (migration `0007_growth`)

- `metric_sources`: one health row per source (status, tier, credential env names, last run, last good run, last
  error, rows, next due). Seeded by `app/seed.py`, never overwritten once edited. The on/off switch is **not** here: it
  is the `app_settings` row `growth_source.<source>` (`on` or `off`), read fresh on every call like the product
  switches. **Every source starts Off.**
- `metric_snapshots`: the one table the screens read. One row per `(date, source, product, metric, dimension)`, the
  unique key `uq_metric_snapshots_key`, so a re-pull upserts the same row. A pull that answers for a whole window of
  some metrics (Stripe, GA4, Search Console, Brevo, Zernio, and each day a product's ledger answers) first clears that
  window's rows of those metrics in the same transaction, so a row whose key moved (a list, profile, property or price
  remapped to another product, a query that dropped out of Google's top rows) does not stay beside its replacement.
  Two rows with one key and different values in one pull are refused, never silently overwritten: collectors add up
  what lands on one key (the www and apex versions of a page, two campaigns with one name, `ppmeta` and `PPMETA`).
  `product` is `pp`, `kp`, `ordris`, `hnh` or `group`; `dimension` is `""` for the total or `kind:value` (`campaign:`,
  `source_medium:`, `channel:`, `promo:`, `list:`, `query:`, `page:`, `device:`, `country:`,
  `account:<platform>:<handle>`, `post:zernio:<id>`, `project:<neon project id>`, `method:`, `kind:`,
  `stream:transactional`), cut at 200 characters before the key is computed. Money metrics end in `_pence` and hold
  pence.
- `ad_spend`: one row per platform campaign per day (unique `(platform, account_id, campaign_id, date)`); product from
  the campaign name's prefix (`pp-`, `kp-`, `ord-`, `hnh-`), else `group`. A CSV with several rows for one campaign
  on one day (ad sets, ads, or a breakdown) is added up into that one row before it is stored.
- `product_purchases`: one row per purchase as a product's partner API reports it (unique `(product, purchase_ref)`).
  `account_key` is a sha256 digest made on the product side; anything else is dropped on read.
- `promo_channel_map`: promo code to channel, platform and campaign, per product; edited on Sources.
- `metric_runs`: one row per collector run (the crons, Pull now, dry runs); `plan` holds the redacted plans of a dry
  run only.
- `sales_attribution` (a view): each purchase, with its status, and one channel, in this order: the promo code's map
  entry; else the last touch's `utm_source`/`utm_medium`; else an affiliate reference; else the first touch; else
  direct. The utm rules are `UTM_RULES` in the migration, mirrored by `app/services/growth/attribution.py` (a test
  holds them equal): `affiliate` source or medium is affiliate; `kpuk`, `propertypredator`, `hnh`, `ordris` are
  partner; a social platform with a paid medium is paid social; `cpc`/`ppc`/`paid_search` is paid search; `email` or
  `brevo` is email; `social` or `zernio` is social; anything else said is other. `facebook`, `fb`, `instagram`, `ig`
  count as the `meta` platform and `twitter` as `x`.

No table holds an email address, an IP address or a per-person history: counts, campaign names, hashed account keys,
Stripe ids and Neon project ids only.

**Days.** Every row's `date` is a calendar day, and the sources do not all count days the same way. London days
(Europe/London, so BST is respected): Stripe, Clerk, the product ledgers, Brevo's new contacts and campaigns, Zernio's
posts, and Channels and the promo note, which count each purchase on its London day. The platform's own day, taken as
given because a daily figure cannot be converted: Search Console (Pacific Time), GA4 (the property's time zone, which
should be Europe/London; the GA4 Test warns when a mapped property's is not), Neon (UTC), and Brevo's transactional
reports and Zernio's daily totals and follower counts (their documentation does not say; read them as UTC). The Search
and Costs screens say so. Read a Search figure beside a sign-up figure day by day with that in mind: they can be up to
eight hours apart.

## The metric registry

`app/services/growth/registry.py` lists every metric a collector may write, its unit and which sources may write it;
the store refuses anything else (`ValueError`, nothing written), so a typo fails a test. The Neon metrics keep Neon's
own units and `NEON_UNITS` turns them into billing units: CU-seconds / 3600 = CU-hours; byte-months / 10^9 =
GB-months (root, child, instant restore, snapshot storage); bytes / 10^9 = GB (public and private transfer);
branch-hours / 744 = branch-months (extra branches) (neon.com/docs/introduction/usage-calculations).

## Sources

| Source | Tier | Variables (web service, copied to the cron) | Calls |
|---|---|---|---|
| `ga4` | daily (realtime on hourly) | `GOOGLE_SA_JSON`, `GA4_PROPERTY_IDS` (`pp=123456789,...`) | per property one `batchRunReports` with four reports; hourly `runRealtimeReport` |
| `search_console` | daily | `GOOGLE_SA_JSON`, `GSC_SITES` (`pp=sc-domain:propertypredator.com,...`) | per site five `searchAnalytics/query` groupings, `today-7` to `today-3` |
| `stripe_mh196` | hourly | `STRIPE_RK_MH196`, `STRIPE_PRODUCT_MAP` (`prod_...=pp,price_...=ordris`) | subscriptions (all, discounts' coupons expanded), checkout sessions, balance transactions, promotion codes |
| `stripe_hnh` | hourly | `STRIPE_RK_HNH` | as above; Hello New Home 2 Ltd has no Stripe account yet |
| `brevo` | daily | `BREVO_API_KEY_HQ`, `BREVO_LIST_PRODUCTS` (`123=kp,456=pp`) | lists, contacts by day (counted, never kept), sent campaigns, transactional reports |
| `zernio` | daily | `ZERNIO_API_KEY`, `ZERNIO_PROFILE_PRODUCTS` (`<profileId>=pp,...`), `ZERNIO_AD_ACCOUNTS` (optional) | daily metrics (`attribution=received`), follower stats, posts, ads timeline |
| `product_pp`, `product_kp`, `product_ordris`, `product_hnh` | hourly | the existing tokens and base URLs | `GET .../metrics?from=&to=` and `GET .../purchases?since=&cursor=` |
| `neon` | daily | `NEON_API_KEY`, `NEON_ORG_ID`, `NEON_PROJECT_PRODUCTS` | project names, consumption history v2, daily, every billable metric |
| `clerk_ordris` | daily | `CLERK_SECRET_KEY_ORDRIS` (not in render.yaml) | `users/count` per London day; shown as Ordris's sign-ups only on days the Ordris ledger has none |

Not built yet, shown on Sources with what they wait on: `google_ads` (packet 3), `meta_ads` and
`meta_pages_instagram` (packet 2), `linkedin` and `tiktok` (packet 4). Until then their spend arrives through the CSV
import, and TikTok/X through Zernio's ad timeline once those ad accounts are connected there.

Every collector is a `MetricCollector` (`app/services/growth/collectors/base.py`), which keeps the connector idiom:
10 to 30 second timeouts, no retries of its own (the next run re-pulls the window), `ConnectorError` with a short
reason, `pull(since, until)` and `probe()`, and a plan of every request with its credential headers redacted
(`Authorization: Bearer [redacted]`, `api-key: [redacted]`, `X-Service-Token: [redacted]`). A platform's error text is
passed through `scrub()` before it is stored or shown, so an echoed key is blanked.

### Google (GA4 and Search Console)

One service account for both, as one environment variable `GOOGLE_SA_JSON` (the key file's JSON on one line; not a
Render secret file, because `fromService` copies environment variables to the crons and secret files would have to be
added to all three services by hand). Viewer on each GA4 property, Restricted user on each Search Console property.
The token is minted by `google-auth` over this app's own httpx client (`collectors/google.py`), always at Google's
token endpoint whatever the key file says; a key file that cannot be used (a wrong file pasted) fails the source or the
Test with `bad_credentials` and its error type only. GA4 sign-ups, leads, trials and purchases are read as `eventCount` of
`sign_up`, `generate_lead`, `trial_start` and `purchase` with a filter (valid before an event is marked as a key
event), so they are the pixel's view: consenting visitors only. A thresholded report marks its rows
`meta.thresholded`. Search Console's CTR and position are stored as Google gives them for the row; every total is
recomputed from clicks and impressions (position weighted by impressions). Google rows that land on one stored key
(the apex, www, http and query-string versions of a page are one `page:/path`) are added up the same way before they
are stored. The GA4 health row (Sources, the Visitors "as of") follows the daily pull only: the hourly realtime read
keeps its own run rows and fails the hourly cron if it fails, but never clears or sets the daily pull's health.

### Stripe

Restricted keys with Read on exactly what the collector calls: Subscriptions, Checkout Sessions, Balance (balance
transactions), Promotion codes and Coupons, plus Products and Prices (the subscription items carry their prices; no
personal data). Everything else None: Customers, Invoices, Charges, Refunds and Events included (Events would carry
customer emails, and nothing here reads them). This is narrower than DASHBOARD-SPEC 3.8's list, which also named
Refunds and Events. Checkout Sessions read does still return each session's `customer_details` (the buyer's email):
the collector drops it on read and never stores it. If Test answers a `permission_error`, it names the resource to add.

Every request sends `Stripe-Version: 2025-09-30.clover`, so the response shapes do not depend on the account's default
version. From that version a discount carries its coupon under `source.coupon`, unexpanded unless asked: the
subscription list expands `data.discounts.source.coupon`, and a coupon that still comes back as an id is read with one
`GET /v1/coupons/{id}`. MRR is computed here: active and past-due subscriptions, every licensed GBP item's
`unit_amount x quantity` normalised to a month (month / count, year / 12 / count, week x 52 / 12 / count, day x 365 /
12 / count), net of a live percent or amount discount, labelled `net_of_discounts`; trials are 0 until they pay. One
snapshot a day, so churned MRR is the difference of two days. A new payer (`paying_new`) is counted on its trial end,
else its start, and never for a subscription that has not paid: trialing, incomplete, incomplete_expired, paused (a
trial that ended without a payment method), or cancelled for a failed payment before its first billing period could
have ended (Invoices are not read, so this is the rule; a past-due subscription counts, as it does in Stripe's own
figures). Products: subscription `metadata.product`, else the price's `metadata.product`, else `STRIPE_PRODUCT_MAP`,
else `group` (MH196) or `hnh`. Revenue and refunds come from balance transactions (gross, GBP, account-wide).

### The product endpoints (what each product must expose)

Same auth as each product's existing Group HQ routes (Property Predator `X-Service-Token`, the others Bearer and
`X-Partner-Client: grouphq`), JSON errors `{ok:false, reason, message}`:

    GET /api/partner/grouphq/metrics?from=2026-11-01&to=2026-11-14      (PP, Ordris; KP and HNH: /api/partner/metrics)
    {"ok": true, "product": "pp", "days": [{"date": "2026-11-13", "sign_ups": 12, "sign_ups_by_method": {"email": 9},
      "leads": 4, "leads_by_kind": {"valuation": 3}, "trials_started": 0, "trials_active": 0, "paying_new": 2,
      "paying_active": 31, "cancelled": 0, "mrr_pence": 68900, "revenue_pence": 3800, "refunds_pence": 0,
      "purchases": 2, "reservations": 0, "instructions": 0, "promo_redemptions": {"PPMETA": 1}, "affiliate_sign_ups": 1}]}

    GET /api/partner/grouphq/purchases?since=2026-11-01T00:00:00Z&cursor=   (KP: /api/partner/purchases;
                                                                              HNH: /api/partner/instructions)
    {"ok": true, "purchases": [{"ref": "cs_live_...", "occurred_at": "...", "kind": "subscription", "plan": "pro_buyer",
      "amount_pence": 1900, "currency": "gbp", "status": "paid", "promo_code": "PPMETA", "affiliate_ref": "",
      "first_touch": {...}, "last_touch": {...}, "account_key": "<sha256 of person_key>", "stripe_customer_id": "cus_..."}],
     "next_cursor": null}

A day the product cannot compute is omitted, not zero, and so is a metric missing from a day. Hello New Home's
instructions may come under `"instructions"` instead of `"purchases"`; `kind` defaults to `instruction` there and to
`reservation` for Key Properties. A purchase whose kind or status Group HQ does not know is skipped with a note.

### Neon: running costs

`GET https://console.neon.tech/api/v2/consumption_history/v2/projects?org_id=&from=&to=&granularity=daily&metrics=...`
with every billable metric (`compute_unit_seconds`, `root_branch_bytes_month`, `child_branch_bytes_month`,
`instant_restore_bytes_month`, `public_network_transfer_bytes`, `private_network_transfer_bytes`,
`extra_branches_month`, `snapshot_storage_bytes_month`), paged by `pagination.cursor`, plus `GET /projects?org_id=`
for the projects' names. The response is `{"projects": [{"project_id", "periods": [{"period_id", "period_plan",
"period_start", "consumption": [{"timeframe_start", "timeframe_end", "metrics": [{"metric_name", "value"}]}]}]}],
"pagination": {"cursor"}}` (Neon API reference, `getConsumptionHistoryPerProjectV2`). Daily figures are kept by Neon
for 60 days, so the daily cron re-pulls 35 days (this month and the end of the last). One row per project per day per
metric, product `group`, dimension `project:<id>`; the days are Neon's, UTC.

`NEON_PROJECT_PRODUCTS` labels projects: `projectId=pp,projectId=kp,projectId=hnh,projectId=grouphq,projectId=ordris`.
A label is free text, because the organisation also holds projects outside the four products (the two Growth HQ
production projects, a disposable test project, AlphaStack): `pp`, `kp`, `ordris` and `hnh` show the product's name;
`grouphq` shows as Group HQ; any other label shows as typed; a project with no label shows under its newest Neon
project name, and one the projects list no longer returns under its id. No project is dropped. Labels are applied
when a screen is read, from the current `NEON_PROJECT_PRODUCTS`, and are never part of a row's key: label your
projects whenever you like (after seeing their names, say) and each project stays one line, its usage counted once. A metric Neon adds later is named in the run's notes until the registry prices it.

**The Neon key.** `NEON_API_KEY` is an organisation API key, and an organisation key can change and delete projects,
not only read them. It is therefore set on the web service and copied to the **daily** growth cron only (never the
hourly one or any other cron), it is used for two reads, and it is never logged, stored, shown on a page or put in a
plan (`Bearer [redacted]`). Rotate it in the Neon console if it is ever exposed.

**Rates.** Settings > Running costs holds what your Neon plan charges per unit, in US dollars, one field per billing
unit. Every rate starts blank and Group HQ never assumes one: until a rate is set the Costs screen and the Overview
panel show the usage with "set your plan's rates" and no estimate. With rates set, the estimate is usage x rate before
any allowance your plan includes; the invoice is the truth. Every change is a `settings.running_cost_rates` audit row.

### The CSV import

Sources > Import a CSV takes a daily campaign export from LinkedIn Campaign Manager, Meta, TikTok, Google Ads or X and
writes `ad_spend` rows (`raw.via = "csv"`), the same rows the API collectors will write. Columns are found by their
header; title lines above the header are skipped; dates are ISO unless the form says day-first or month-first (the
importer will not guess between 03/11 and 11/03); a totals line is skipped; one unreadable row refuses the whole file.
Several rows for one campaign on one day (an ad-set or ad-level export, or a breakdown by age, placement or device)
are added up into one campaign-day, so the dry run shows exactly the rows that will be written. Importing the same
days again replaces them. Tick dry run to see the rows first. The upload is read only after the login and origin
checks, and refused by its size first (2 MB). Audit `growth.csv_import`.

## The job and the crons

    python -m app.jobs.growth_pull --tier hourly|daily|all [--source NAME ...] [--since YYYY-MM-DD] [--dry-run]

Per source that is On and runs in the tier: pull its own window (or from `--since`, daily tier only), write in one
transaction, record a `metric_runs` row and the health row. One line per source (`ok ga4 412 rows 2.1s`, `failed brevo
unauthorized (HTTP 401): Key not found`, `dry-run neon 2 requests planned, nothing sent or written`), a summary line,
one `growth.pull` audit row as `job:growth` with the tier and, per source, whether this job held its variables (none
at all when every source was Off). One source that raises past its own guards (a database error writing its run, say)
is rolled back, printed as failed and counted; the job goes on to the next. Exit 1 when a source that is On failed, or
is On but not configured in this job's own environment (the warning names the variable), and exit 1 with nothing done
when the database is not at the build's Alembic head. A source switched Off is skipped silently.
`CONNECTORS_DRY_RUN=1` makes every pull a dry run (plans printed and kept, nothing fetched, nothing written); its
per-product list form and the product switches govern what is sent to a product, not these reads.

`render.yaml`: `group-hq-growth-hourly` at `7 * * * *` (Stripe, the product ledgers, GA4 realtime) and
`group-hq-growth-daily` at `10 6 * * *` (GA4 after the Pacific-midnight quota reset, Search Console, Brevo, Zernio,
Neon). Render can copy only a variable the web service holds, and a `fromService` entry naming one it does not hold
fails the Blueprint sync, so the entry always follows the variable. Both crons therefore ship copying only what the
web service holds today: `DATABASE_URL`, `SECRET_KEY`, `BASE_URL`, `CONNECTORS_DRY_RUN` and, on the hourly one, the
four products' base URLs and tokens. Each Growth variable gets its `fromService` line on the cron that reads it, in a
later commit, once it is set on the web service (`render.yaml` lists which line goes on which cron). `STRIPE_RK_HNH`
waits for Hello New Home 2 Ltd's Stripe account and `ZERNIO_AD_ACCOUNTS` for an ad account connected in Zernio; leave
both out until then. Deploy both crons from the same commit as the web service. `CLERK_SECRET_KEY_ORDRIS` is
deliberately absent: Clerk's backend key can delete Ordris users; Ordris's own metrics endpoint gives the same count
with no new secret. If it is ever needed, add it to the web service and the daily cron by hand, switch `clerk_ordris`
on, and remove both the day the Ordris endpoint answers.

## Run book

1. **Before 0007 deploys**: production is at `0006_admin_switches` (the pre-deploy step runs `alembic upgrade head`).
   0007 only adds tables and a view; `alembic downgrade -1` drops the view, then the tables.
2. **Start a source**: set its variables on the web service (dashboard); add their `fromService` lines to the cron
   that reads them in `render.yaml` (the list above the growth crons), commit and sync the Blueprint; press **Test**
   on Sources (one cheap authenticated call; under `CONNECTORS_DRY_RUN=1` it shows the redacted plan instead; GA4 and
   Search Console also name any property or site the key cannot see, and GA4 any property not on London time), then
   switch it **On**. **Pull now** runs it at once for its window; **Dry run** shows the plan.
3. **A cron run shows failed on Render**: open Sources. The source's row names its last error and, from the latest
   real run of each cron that ran it, whether that cron held its variables. Either add the variable's `fromService`
   line to that cron and sync, or switch the source Off.
4. **Backfill**: `python -m app.jobs.growth_pull --tier daily --source ga4 --since 2026-11-01` from a Render shell on
   the daily cron (GA4 keeps 14 months once set, Search Console 16, Stripe for ever, Zernio 366 days, Neon's daily
   figures 60 days).
5. **Map promo codes** on Sources as each code is issued (one code per channel per product). An unmapped code is
   flagged on Channels and its sales fall back to their UTM touch.
6. **Set the Neon rates** on Settings once you know your plan's prices.
