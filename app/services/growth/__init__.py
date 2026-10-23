"""Growth: one screen fed by APIs (visitors, sign-ups, trials, paying, MRR, ad spend, cost per sale, email, social,
search and running costs), by product and channel.

    registry.py      every metric a collector may write, with its unit (a test holds the collectors to it)
    store.py         the upserts (metric_snapshots, ad_spend, product_purchases), idempotent on each table's key
    collectors/      one read-only collector per platform, in the connector idiom (dry run, redacted plans)
    runner.py        run one collector and record the run (metric_runs, metric_sources, the rows)
    attribution.py   the channel rules the sales_attribution view applies, in Python
    reports.py       the queries the screens read (Postgres only: no page view ever calls a platform)

docs/growth.md is the module's contract and run book.
"""
