# GHQ-INBOX-004D: available private one-to-one chats

Assigned by Root on explicit founder request; Astra ratified the identity/privacy
boundaries. Exact base: LinkedIn package `4a48f4db18df2ecec2aba2a3dfdf6ee91c971800`
above live Group HQ `83639933620c4a32a5c45dadb8f623fdd642b4ac`.

Outcome: owner can choose **Sync all available chats** at `/inbox/discover` for
their enabled native Messenger connection. This includes incoming and personal
one-to-one chats, without requiring a search phrase. It does not select a
business, create contacts, subscribe anyone to marketing, or send a message.

Allowed areas: existing native discovery service/router/template, desktop
discovery/runtime, focused tests and this receipt. No source edits to the shared
inbox triage, no production settings, runtime restart, credential access,
provider/network messages, migration, alternative architecture or other channel.

Root's subsequent bounded snapshot addendum also permits `app/services/inbox.py`:
expose up to 2,000 local metadata rows per account for Root's route pagination,
batch owner choices and identity-bound CRM-link brands in 500-target queries,
and add `owner_choice` to each thread. The existing `organisation` dictionary
and existing filters remain unchanged; message bodies are not selected for the
snapshot. Root owns triage filtering and route/template pagination.

Snapshot validation checkpoint: expanded local metadata and removed per-thread
SQL -> three focused batching/owner-choice/identity/bound regressions plus
existing inbox and personal-inbox neighbours -> expected one minute on local PG16.
The first combined snapshot run's process handle/output was not retained by the
tool wrapper; it is not counted as a pass. Smallest evidence rerun: the three
new snapshot tests and four classification/owner neighbours, retaining the tool
process handle/output -> expected 20 seconds. That run exposed a test collection
import path error (`tests.test_personal_inbox` is the package path); corrected
that test import and reran the same focused seven. No application code changed
for this rerun.
Final snapshot addendum result: seven focused Postgres regressions passed,
zero failures/skips, in 8.43 seconds; changed Python lint passed. This proves
521 imported metadata rows are exposed using at most seven SELECTs, explicit
owner choices retain the exact dictionary, foreign CRM identity is excluded,
and a 2,001-row local account retains the 2,000-row overflow signal. Root's
route/template view pagination is not part of this package.

Validation checkpoint: changed rendered-list batching, owner scope and import
permission -> focused native Node tests plus local-Postgres personal-inbox and
six pending public-comment regressions, changed-file lint -> expected 2 minutes.
Root supplied a disposable local PG16 database; no production DB is used.

The existing owner/account discovery registry stores an explicit `sync_all`
flag. Enabling requires the owner's authenticated same-origin POST and explicit
private-chat acknowledgement; an audit event records the flag change. Manual
selections are retained separately. Turning the flag off immediately returns
ingestion and pending-reply authorization to those prior selections.

Discovery reads only actually rendered Facebook inbox-list rows, including
incoming/personal previews. It does not filter by outgoing text or a phrase.
Each new target is opened in one reusable peer page and verified through the
exact current thread's unique conversation log and participant profile ID.
Unknown participants, ambiguous/groups, unsupported formats and identity changes
retain the existing rejection boundaries. Discovery reports metadata only;
bodies enter the private inbox only for verified permitted registry entries.

One discovery list page keeps its rendered position across batches of ten new
targets. One peer page verifies those targets. At a stable visible boundary it
closes both and starts a new pass after one minute. Previously verified targets
are skipped during discovery. The collector rechecks its Facebook owner identity
after discovery and before importing a body. It reuses one conversation read
page for all targets and round-robins at most 20 targets per main loop, rather
than leaving a Chromium tab open per conversation. Existing phrase search keeps
its own temporary pages and named-selection behavior.

Bounds: each discovery report remains at most 100 candidates; the existing JSON
registry now holds up to 2,000 distinct candidates. Full archival history and
guaranteed access to every historic chat are not built. Facebook's rendered,
restored and supported windows determine availability; coverage remains
`complete=false`. Existing 100-message window, attachment/date parsing and exact
native message-ID rules remain unchanged. No auto-resend or automated outreach
is introduced.

Cutover: Root integrates after the LinkedIn package and passes the exact release
gate; deploy the compatible server before replacing desktop files. Owner then
enables sync-all through the dashboard, or Root records the founder's existing
explicit instruction in the same audited action. Retain the current named
configuration as fallback; the runtime no longer treats it as the sync-all
discovery source after registry seeding.

Recovery: turn off automatic discovery; named selections remain. Pause the
desktop collector before replacing its files, then restart the prior compatible
collector if necessary. Do not remove cached conversations, registry entries or
reply receipts. No other channel or customer journey is paused by this feature.

Focused results: 28 Postgres/Python regressions passed (six public comments and
22 personal inbox, including six new sync-all tests), zero failures/skips. The
first sandboxed attempt could not open the local test socket; the authorized
local-only elevated rerun passed in 38 seconds. Node native tests cover rendered
incoming/personal rows, incremental viewport batches, verified/unknown peers,
skipping known peers, fixed read-page reuse, round-robin coverage, compatible
seeding and unchanged identity/send journal neighbours: 30 passed, zero
failures/skips. Changed Python lint and diff whitespace checks pass. The normal exact
integrated release gate and actual owner walkthrough remain Root's work.

Retained unchanged-area proof: 18 LinkedIn adapter/ticket/template tests and 14
existing transport tests at the first package's tree, all passing with no
failures/skips. Native changes do not modify those adapter/ticket files.

Status before integration: source package only; no live discovery, import,
runtime restart, send or production configuration change performed by this
agent. Test results and exact candidate are reported to Root separately.
