# GHQ-INBOX-005 native personal Messenger archival slice

Root assigned on the founder's years-of-history/no-lifetime-limit instruction.
Exact fresh-main base bca02b8067a95d0026e36478001349b3fc54f2aa. Own branch
codex/ghq-native-history-20261109. Root owns archive storage/ingestion and the
single integrated release; 005C owns the shared client history allowlist.

Allowed: tools/native-messenger reader/discovery/runtime, focused Node tests and
this receipt. No independent deployment, user desktop control, provider sends,
CRM projection, other connector implementation or shared server edits.

The current 100-message recent snapshot and relative-date parser are code limits,
not evidence Facebook lacks older messages. Native target validation also caps
total selected conversations at2,000. The all-available list already advances
between temporary batches; its per-batch limit is not an archive lifetime cap.
Actual older Facebook availability depends on the restored encrypted session and
rendered UI, and has not been established by fixture tests.

Contract agreed with Root: signed `history` action on the same connector route;
existing account/owner/thread/participant binding, messages and observation time;
chunk_id is64 lowercase hex, reached_start boolean, oldest_id/newest_id match
chronological message edges (null for empty). Matching ACK precedes encrypted
local cursor advancement. Existing evidence.omitted_rows and thread.has_more
remain explicit. Historical chunks never go to `threads` or replace latest
state. Root can register verified selected metadata directly in history.

Selections paging uses limit250, cursor, next_cursor; missing/null cursor means
end and legacy omitted cursor remains compatible. No native total-target cap.

Changed reader/history cursor and date support -> >100-message/multi-year,
chunk-byte-bound, lost-ACK/resume, owner/participant, genuine-boundary and UI
scroll fixtures plus unchanged recent/send neighbours -> expected15minutes
implementation and focused proof checkpoint. Normal exact integrated gate and
actual isolated-owner old-history walkthrough stay with Root.

## Source-complete checkpoint, 9 November

Native source has no lifetime selected-conversation, discovery-result or archive
message count limit. Recent reads remain the compatible 100-row window. Archive
requests contain at most 100 messages and 500,000 JSON bytes each; these are
transport batch bounds, not historical totals. One separate archive page scrolls
the rendered transcript backward. An ACKed native-ID anchor in the existing
encrypted journal survives restart; restart seeks actual overlapping native IDs.
A lost ACK replays evidence idempotently rather than advancing the cursor.

Explicit-year dates and full-year date headings support minute precision in the
configured Europe/London timezone. Ambiguous DST minutes, unsupported/no-year
old dates and non-text rows are omitted with evidence. A later unsupported date
heading clears earlier date context. Visible introduction plus the oldest scroll
edge is reported as reached_start only when no restoration prompt is visible;
quiet/stalled loading is older_boundary_unconfirmed and retries. This reports
the available UI boundary, not deleted or unavailable encrypted material.

Named phrase searches stream verified metadata in batches of 100 with
request_done false until their final packet. Selections page in batches of250
without a lifetime ceiling. Parent must retain the pending request across those
partial packets. The signed history client seam is dependency
42ab243a71dea72026363a1ecc128e25e8cad51e from 005C; not duplicated here.

Checks actually run: first native run 44 pass/0 fail/0 skip. The subsequent
date-context correction had its one focused test pass, followed by the final
native gate: **45 pass/0 fail/0 skip**, 15 archive additions and30 unchanged
recent/discovery/send neighbours, about20 seconds. Command:
`node --test tools/native-messenger/test.cjs tools/native-messenger/archive.test.cjs`.
NODE_PATH reused the unchanged pinned Playwright dependency from the existing
inbox-triage worktree. `git diff --check` passed. No provider/production calls,
database tests, live UI import or send were performed for this slice.

Root must add archive.test.cjs to the explicit integrated CI Node invocation
(npm test in this native package already includes it), integrate the server
history receiver and 005C client, then use the existing reviewed operator pause,
journal-settle, exact-source restart and resume procedure. Preserve the encrypted
profile and journal, including creds.history; do not run a second profile owner.
Recovery: pause, settle any existing send journal, restart the previous verified
runtime with the same private state and resume. Stored server archive evidence
is retained; it never overwrites current threads or authorises outbound work.

Status: source-complete and fixture-proven. Integration, ratification, publication
and real available years-of-history proof belong to Root's single release.
