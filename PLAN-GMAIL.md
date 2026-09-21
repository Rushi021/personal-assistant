# Implementation Plan — Gmail ingest (Phase 1b)

Companion to [PLAN-V1.md](PLAN-V1.md) §6 and [PLAN-EXPENSES.md](PLAN-EXPENSES.md).
Logging spec in [evaluation_plan.md §4 and §9](evaluation_plan.md).

Two files against the D3 contract: `capabilities/gmail.py` and
`capabilities/gmail_parse.py`. Core gains two queries; `app.py` gains one loop.

**Shipped.** This document records what was settled and where the seams are, so
the next lane is an edit rather than a re-argument.

---

## 0. What this does, and what it does not

| Lane | Emails | Writes |
|---|---|---|
| **A** | Credit-card **bill payment** confirmations | `expenses` row, `kind='cc_payment'`, `status='confirmed'` |
| **B** | Job application receipts and rejections | `applications` row, `applied` or `rejected` |
| **C** | Card **swipe alerts** — "you spent $42 at…" | `expenses` row, `kind='spend'`, `status='pending'` |

A card bill payment is a **settlement**, not a spend. It is stored and excluded
from every spending total by the `kind` filter (PLAN-EXPENSES.md E7). Reading
one as a spend double-counts your entire month, silently — which is why
`route()` checks the payment family before the spend family, and why that is
the single most important ordering in the file.

Lane C rows land as `pending`: the amount, card and date count immediately,
and the meaning is blank until you answer the digest (E2, E6). That is not a
half-written row — it is a complete fact about money with a question attached.

Not built: SMS ingestion (E5), interview-invite parsing, merchant→category
memory, Gmail Pub/Sub, and any LLM call at all.

---

## 1. The workflow

```
/cron/tick (hourly)
   │
   ├─ gmail.configured()?  ── no ─► skip entirely
   │
   ▼
messages.list  q="newer_than:2d -in:chats -in:sent -in:drafts"
   │                                     ~100 ids, most already handled
   ▼
db.seen_message_ids(user, ids)           ONE query, not N
   │
   ▼  the handful that are new
messages.get(id, format=full) ─► decode text body (never stored)
   │
   ▼
route(sender, subject, body)
   ├─ payment subject family      ─► Lane A ─► expenses (cc_payment, confirmed)
   ├─ spend-alert subject family  ─► Lane C ─┬─ same amount in chat, last 60min?
   │                                         │     yes → discard, log it (E2)
   │                                         └─► expenses (spend, pending)
   ├─ receipt OR rejection phrase ─► Lane B ─► applications (upsert)
   └─ none of them                ─► ignored
   │
   ▼
email_events row — ALWAYS, including ignore, parse failure and error
```

No model is called anywhere on this path. `capabilities/gmail_parse.py` is pure
regex over normalised text, and `test_gmail.py` asserts neither file reaches for
`core.turn` or a provider endpoint.

---

## 2. Decisions

### G1 — A fixed window, not a historyId cursor ✅

The brief called for `history.list` with a stored cursor per user, falling back
to a bounded `messages.list` when the cursor expires. That fallback is the
reason the cursor was dropped: Gmail has no history on a first run and discards
it after roughly a week, so the catch-up query gets written either way — and
then the cursor is a *second* path whose failure mode is a **silently skipped
window**.

`newer_than:2d` every tick, deduped by `email_events`, is one path with no
state to drift. The overlap is not waste: `seen_message_ids` is one indexed
query, so the steady state is ~2-5 `messages.get` calls an hour.

**What it costs:** an outage longer than two days loses the gap. Widen `QUERY`
in `capabilities/gmail.py` if that ever happens.

**Add the cursor when:** you poll many mailboxes and the re-listing cost
becomes real. Not before — one mailbox at one call an hour is nowhere near any
Gmail quota.

### G2 — Two dedupe layers, one of them free ✅

1. **`email_events (user_id, gmail_message_id)` unique** — the poll claims a
   message by inserting here. This is the layer that matters, and it covers
   ignores and parse failures too, which no per-lane key can.
2. **`expenses (user_id, source_ref)` unique** — the backstop. A payment cannot
   produce two rows even if the ledger were bypassed. A `23505` here is
   swallowed as success: the row exists, which is the outcome wanted.

`applications` deliberately has **no** unique index on `source_ref`. That column
names the email that *last moved* the row and is overwritten on every update, so
uniqueness on it would reject the very writes the table exists to record.

Gmail's UNREAD flag is not used, and messages are never marked read. The
mailbox is read-only (`gmail.readonly`) and the dedupe is ours.

### G3 — Rules, not a model ✅

Both lanes are a fixed family of templates. `evaluation_plan.md` §9 imagined a
Claude call per email; that is real money and real latency for text a regex
reads exactly. An email the rules cannot read is recorded as `unparsed_payment`
or `unparsed_application` and **nothing is escalated**.

Those two outcomes are the extension point. When enough of them pile up, the
choice is: add the pattern (cheap, precise) or send just those to a model
(expensive, general). Query them with:

```sql
select subject, sender, count(*) from email_events
 where outcome like 'unparsed%' group by 1,2 order by 3 desc;
```

### G4 — Any rejection phrase wins ✅

Three cases, not two:

| Email | Status |
|---|---|
| Receipt phrase, no rejection phrase | `applied` |
| Receipt phrase **and** rejection phrase | `rejected` |
| Rejection phrase, **no** receipt phrase | `rejected` |

The third is the common real shape — "Update on your application at Acme" —
and a receipt-versus-rejection framing misses it entirely.

Rejection phrasing frequently sits in the **body** under a neutral subject, so
both are always scanned.

### G5 — The praise guard is an absence ✅

`skills and background are impressive` appears in rejections and in
we're-still-reviewing emails alike. On its own it means nothing; the rejection
is carried by the proceed/pursue/move-forward clause, so that is what
`REJECTION_PATTERNS` matches. No praise phrase is in the list, and
`test_rejection_patterns_contain_no_praise` keeps it that way — a test rather
than a combiner function that exists to undo a bad entry.

### G6 — `applications` is keyed on company, not (company, role) ✅

Rejection emails routinely name a different role string from the receipt, or
none at all. Keying on role would guarantee the rejection never finds the
application it exists to close — the one job of the lane.

`company_key` is the match: lowercased, punctuation dropped, **legal suffixes
only** stripped (`Inc`, `LLC`, `Ltd`, …). `Labs`, `Group` and `Technologies`
survive, because they distinguish genuinely different companies more often than
they are noise.

The index is **not unique**: applying to one company twice must stay
expressible. Which row an email attaches to is decided in
`_upsert_application`, where it can be read:

- **rejection** → the newest still-`applied` row for that company; none → insert
- **receipt** → the row with the same role (or no role on either side); none → insert

### G7 — An orphan rejection becomes a row ✅

A rejection with no matching application — the receipt predates this feature, or
never came — inserts a row anyway: company from the subject or sender, `role`
null, `status='rejected'`, `last_email_at` set. An orphan you can see beats a
rejection you never hear about. `evaluation_plan.md` §9 says to optimise recall,
and a wrong row costs one correction while a dropped rejection costs nothing
visible at all.

If no company can be extracted, nothing is written and the outcome is
`unparsed_application`.

### G8 — The sender is a hint, not a gate ✅

The brief gated Lane B on a noreply-ish sender. Rejections arrive from
`careers@`, from named recruiters and from no-reply alike, so that gate drops
real ones. If a receipt or rejection phrase fires, the email routes to Lane B
whatever the sender.

The sender still does two jobs: it is one rung of the company ladder, and
`ATS_DOMAINS` is how `greenhouse.io` is recognised as the sender rather than the
employer.

### G9 — Bank labels resolve through `users.cards`, and only through it ✅

No second allowlist. `users.cards[*].match` (PLAN-EXPENSES.md E8) is already the
single source of truth for which strings identify which card, so Lane A matches
against it — **longest match string first**, because `Chase` and `Chase Freedom`
are both in the config and the shorter one would otherwise claim every Freedom
email for the debit account.

No match means `card = null`, a log line naming the label, and the row is still
written. A wrong card silently corrupts E9's reconciliation; a null card is
merely incomplete, and visibly so. The log line tells you what to add to the
config.

### G10 — `spent_at` comes from the email ✅

The date stated in the payment confirmation. When the email names none, Gmail's
own `internalDate` — the honest fallback, and unlike `now()` it does not drift
with when the poll happened to run (PLAN-EXPENSES.md §4).

### G11 — Raw REST over `httpx`, no Google SDK ✅

`capabilities/calendar.py` already hand-rolls CalDAV with a one-time
`python -m capabilities.calendar` discovery step. Gmail is easier: a
refresh-token grant and two GETs, about thirty lines, against the `httpx`
already in `requirements.txt`. `google-api-python-client` would have pulled six
packages into a four-dependency repo to save them.

`requirements.txt` is unchanged, and a test asserts it stays that way.

### G12 — Tokens are env vars, one per mailbox ✅

One OAuth client, several accounts, discovered by prefix:

```
GMAIL_TOKEN_PERSONAL=1//0g...
GMAIL_TOKEN_WORK=1//0g...
```

`accounts()` scans the environment for `GMAIL_TOKEN_*` and the label is the
suffix, lowercased. Adding a third mailbox is one line in `.env` — no code, no
migration, no list of accounts to keep in sync anywhere.

A JSON blob in one variable was the alternative and is a trap: `set -a && . ./.env`
runs the file as shell, and `{"a":"x","b":"y"}` brace-expands into two words
before Python ever sees it.

**The label is not decoration.** A Gmail message id is unique *within* a
mailbox; nothing promises two accounts never mint the same one. So every
stored id is `<label>:<id>` — `email_events.gmail_message_id` and
`expenses.source_ref` both — which makes the uniqueness those indexes promise
hold across accounts rather than only within one. Two mailboxes returning the
same bare id stay two rows.

Each mailbox is polled and caught separately: a revoked token or a rate limit
on one is logged as a `poll` step with `ok:false` and the others still run.
Access tokens are cached per label, so it is one refresh per mailbox per hour.

**Add `users.gmail_accounts` when** a second *person* uses this — that is the
axis env vars cannot carry. Token loading is one function (`_access_token`),
so it stays a contained change.

---

## 3. Privacy

`evaluation_plan.md` §4 is a trust boundary, not a preference.

| Stored | Never stored |
|---|---|
| `gmail_message_id`, sender, subject | the body, the snippet, raw HTML |
| extracted scalars in `detail` (amount, date, card, company, role) | OTPs, full PANs, account numbers |

The body is decoded in `_text()`, handed to the parsers, and dropped when the
loop moves on. `test_no_email_body_reaches_the_database` greps every DB write in
`capabilities/gmail.py` to keep it that way.

The audit trail adds one `audit_log` row **per poll**, not per message —
`trigger='gmail'`, `path='rules'`, `model` null — with one `email` step per
message carrying its lane and outcome. Everything written there passes through
`audit.scrub()` like any other turn.

---

## 4. Setup

```bash
# 1. Migrations, in the Supabase SQL editor, in order
#    migrations/003_expenses.sql   (expenses + users.cards)
#    migrations/004_email.sql      (email_events + applications)

# 2. A GCP project with the Gmail API enabled and a Desktop-app OAuth client.
#    console.cloud.google.com -> APIs & Services -> Credentials
#    Put the id and secret in .env:
#      GMAIL_CLIENT_ID= / GMAIL_CLIENT_SECRET=
#
#    Two settings on the consent screen decide whether this works at all,
#    and both fail in ways that do not name themselves:
#
#    User type = EXTERNAL. Internal restricts consent to accounts inside the
#      project's Workspace org, so a @gmail.com address is refused with
#      "Error 403: org_internal".
#
#    Publishing status = IN PRODUCTION. While it is "Testing", Google expires
#      every refresh token after SEVEN DAYS — the poll runs fine and then dies
#      a week later with invalid_grant. Publishing asks for verification
#      because gmail.readonly is a restricted scope; skip it. The app stays
#      unverified, consent shows "Google hasn't verified this app" (click
#      Advanced -> Go to ... ), and unverified production apps are capped at
#      100 users. Tokens are long-lived, which is the point.
#
#    Add every address you plan to connect as a test user.

# 3. One-time consent, ONCE PER MAILBOX. Sign in to the browser as that
#    account first — the consent screen uses whichever Google account the
#    browser already has, which is how you end up with two vars holding the
#    same inbox. Each run prints the variable to paste into .env.
set -a && . ./.env && set +a && .venv/bin/python -m capabilities.gmail personal
set -a && . ./.env && set +a && .venv/bin/python -m capabilities.gmail work

# 4. Edit users.cards so the `match` strings are what YOUR banks actually write.
#    003_expenses.sql seeds the seven cards from PLAN-EXPENSES.md §0 as a start.
```

The poll then rides the existing hourly `/cron/tick`. No new route, no new
secret, no second webhook.

---

## 5. Checks

`python test_gmail.py` — 36 checks on the email rules, plus `test_phase1.py`
for the lanes' write paths and the end-to-end pass. All offline. The four rejection
templates that actually arrived are in there by name, alongside the
praise-only email that must *not* classify as one, the body-only rejection
under a neutral subject, and the double-write checks behind the acceptance
criteria (one `cc_payment` row per gmail id; a rejection closing the row its
receipt opened rather than adding a second).

---

## 6. Not in v1

| Skipped | Add it when |
|---|---|
| Interview-invite lane | You have a month of `unparsed_application` rows to read the templates off. Widen the `status` CHECK in `004_email.sql`, add one pattern family to `gmail_parse.py`, add one branch to `_upsert_application` |
| `oa` / `offer` statuses | Same edit as above |
| LLM classification | The `unparsed_*` pile stops being cheaper to fix with a pattern. Send **only those** — never the happy path |
| ~~Spend-alert extraction~~ | **Shipped** — Lane C. E1 resolved to deterministic rules, which dissolved the model-call-versus-parked-text trade it was blocked on |
| A `historyId` cursor | You poll many mailboxes (G1) |
| Per-user OAuth tokens | A second person uses this (G12) |
| Telling you about any of it | A digest exists to carry it. Today these rows are written and read in Supabase — an assistant that messages you every time an email arrives is one you mute (E4) |
