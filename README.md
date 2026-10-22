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
mentees, payments, entitlement_grants, connector_calls, audit_log, users, plus app_settings for the switches set
on the Settings screen (0006).

The four programmes (mentorship, hnh_franchise, kp_mentee, ordris_pro_bundle) are seeded at boot
with their grant templates and never overwritten; edit them in the app.

## Deploy

`render.yaml` describes the web service (Frankfurt, starter), the nightly `group-hq-recompute`
cron at 03:10 UTC and the `group-hq-statements` cron at 03:40 UTC. Set the secrets marked `sync: false`
in the Render dashboard. The crons copy their settings from the web service, and Render can only copy a
variable the web service actually holds, so `PP_SERVICE_TOKEN` is not copied yet (the Ordris, Key Properties and
Hello New Home tokens are). When a product goes live: set its token on the web service first, then add its
`fromService` entry to the recompute cron and sync, and only then switch it to Live on Settings. `CONNECTORS_DRY_RUN`
takes `1` (everything in dry run), `0` (everything live by default) or a list of products that default to dry
run (`ordris,kp,hnh`, for example) while the others default to live; it ships as `pp`. Since 0006 the list is only the
default: each product has a Live / Dry run switch on **Settings** (side navigation), stored in the database, and
once a product's switch has been set it decides for that product, in the web service and the nightly job alike.
`CONNECTORS_DRY_RUN=1` stays a hard floor that no switch lifts. Switch a product live only once its connector has
been rehearsed with a test account and, for Hello New Home, the data sharing agreement exists. A product without
its token or base URL shows as not configured and sends nothing whatever its switch says. The nightly job has its
own copy of the tokens, so each product's row on Settings also shows whether the job's last real run held its
token, the Live button and its message say plainly when it did not (or when no run has shown it yet), and the job
exits 1, a failed run on Render, when a product that is not in dry run has no token or base URL in its environment.
Deploy `group-hq-recompute` from the same commit as the web service and check its latest build after each deploy:
the job refuses to run (exit 1, nothing sent) when the database is not at its own Alembic head, but a cron still
on a build from before 0006 would ignore the switches and every hold, and rolling the database back past 0006
releases every hold and switch. `ORDRIS_BASE_URL` is `https://ordris.io`, which stays true after Ordris moves to Render.

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
- **Product switches** (`/settings`): Live, Dry run, or back to the environment default, per product. Every change
  is a `settings.product_switch` audit row (who, and the switch and state before and after). The mentee import
  page and the run form show each product's state as the switches leave it. Dry run takes effect at once, a run
  already under way included (from the next person it reaches); Live waits for the next run.
- **Pause and Resume.** Revoke all ends every mentee row of the person, so the nightly rule agrees; a grant
  revoked any other way would simply be granted back by the next recompute while the payments support it. Pause
  is the switch for that: on a grant (its page or the mentee's Grants tab) or for all of a person's access
  (Pause all access on the mentee's Grants tab), it revokes in the product now through the outbox and holds the
  grant, so the recompute leaves it revoked whatever is paid, new grants included for a paused person. The
  mentee rows are left as they are. Resume clears the hold and recomputes the person at once; the re-grant goes
  out under a new idempotency key (`...:after:<the pause's revoke call>`), so the product is asked again. Held
  grants are marked on the mentee, person, grant and Grants pages (filter: held). A grant paused on its own stays
  paused when all access resumes. The reason typed on Pause stays in Group HQ: the product is told only
  `Group HQ: paused (grant <id>)`. A pause whose revoke did not reach the product (failed, given up, blocked or not
  configured) is reported as an error, since the product may still give access; one that gave up needs Retry.
- Login throttling lives in the `login_attempts` table (per address, per address and email, and per
  account), so it survives deploys and cannot be reset by a forged `X-Forwarded-For`.
