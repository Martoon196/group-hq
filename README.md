# Group HQ

The founder's private admin app for MH196 2026 Ltd and Hello New Home 2 Ltd. It records mentees and
what they have paid, and grants or revokes their access across Property Predator, Ordris, Key
Properties and Hello New Home by calling each product's server-to-server API. It holds emails and
ids only; it never reads another app's database.

Stack: Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Jinja2, Postgres (Neon in production, local
Postgres 16 for tests; SQLite is not supported). Login is email, password (argon2) and an
authenticator code (TOTP) on every login. The layout and palette follow Hello New Home HQ.

## Run it locally

```sh
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env            # fill in DATABASE_URL at least; tokens can stay empty with CONNECTORS_DRY_RUN=1
./run.sh                        # migrates to head, seeds, serves on http://127.0.0.1:8030
```

The first boot creates the admin named by `ADMIN_EMAIL`. If `ADMIN_PASSWORD` is empty a password is
generated and printed once to the log. Either way the first login must change it, so clear
`ADMIN_PASSWORD` from the environment (and the Render dashboard) after that first boot. The first
login shows a QR code to enrol an authenticator app; the secret is stored only after a valid code.

## Tests

```sh
DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5432/grouphq .venv/bin/pytest -q
```

The suite refuses to run without `DATABASE_URL`, refuses a non-local host, and works in a database
named `<database>_test` that it drops and recreates, so the named database is never touched.

## Schema

Alembic owns the schema: `alembic upgrade head` runs as Render's pre-deploy command and again at
boot. Change `app/models.py`, then `alembic revision --autogenerate -m "..."` and review the file.
The tables are the ones in the plan: people, organisations, person_organisations, programmes,
mentees, payments, entitlement_grants, connector_calls, audit_log, users.

The four programmes (mentorship, hnh_franchise, kp_mentee, ordris_pro_bundle) are seeded at boot
with their grant templates and never overwritten; edit them in the app.

## Deploy

`render.yaml` describes the web service (Frankfurt, starter), the nightly `group-hq-recompute`
cron at 03:10 UTC and the `group-hq-statements` cron at 03:40 UTC. Set the secrets marked `sync: false`
in the Render dashboard. The crons copy their settings from the web service, and Render can only copy a
variable the web service actually holds, so the product tokens are not copied yet: when a product goes
live, set its token on the web service first, then add its `fromService` entry to the recompute cron and sync. `CONNECTORS_DRY_RUN`
takes `1` (everything in dry run), `0` (everything live) or a list of products to keep in dry run
(`ordris,kp,hnh`, for example) while the others go live; it ships as `1`; take a product off the list only once
its connector has been rehearsed with a test account and, for Hello New Home, the data sharing
agreement exists. `ORDRIS_BASE_URL` is `https://ordris.io`, which stays true after Ordris moves to Render.

## Guards worth knowing about

- A live recompute is always as of today: the as-of date on the Grants page and `--today` on the job
  are accepted in a dry run only.
- No grant or extension is ever sent with an end more than 400 days away or not in the future, whatever
  the caller asked for; the grant shows why on the Grants page.
- A payment period end more than 400 days away, or a monthly period longer than a month and a week, is
  refused on the form.
- A programme may carry a price; a mentee paying in full gets the term once cleared payments reach it.
- A person's email cannot be changed while they hold an active or pending grant: revoke first.
- **Existing users to mentees** (`/mentees/import`, linked from Mentees and the side navigation) enrols up to 25
  people at once: Key Properties sourcers ticked from a live list, plus pasted lines (`email` or `name, email`) for
  anyone else, such as people who only have an Ordris login. One programme, payment mode, start date and optional
  payment apply to the whole batch. A line that cannot be read stops the batch before anything is written. Each
  person is looked up in every live product and only empty product ids are filled; anyone already a mentee on the
  programme (whatever the status) is skipped and named; with Grant access now ticked and a cleared payment, the
  mentee's Recompute runs. Every write is audited, and the batch itself is a `mentee.import` audit row.
- Login throttling lives in the `login_attempts` table (per address, per address and email, and per
  account), so it survives deploys and cannot be reset by a forged `X-Forwarded-For`.
