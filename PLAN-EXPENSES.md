# Implementation Plan — Expense tracking (Phase 1c)

Companion to [PLAN-V1.md](PLAN-V1.md). The workflow was settled across the
interviews of 2026-09-13 and 2026-09-14; the schema and logging spec live in
[evaluation_plan.md §10](evaluation_plan.md).

One file against the D3 contract: `capabilities/expenses.py`. Core is untouched.

Testing and evaluation are a separate conversation; nothing here specifies a
check. Where a step states an expected behaviour it defines what the code must
do, not how it will be proved.

---

## 0. Settled

### Categories — eleven, enforced by a CHECK constraint

```
food · groceries · transport · housing · subscriptions
shopping · health · travel · entertainment · fees · other
```

`food` is eating out, `groceries` is buying to cook — they move independently
and only one is discretionary. `housing` covers rent or mortgage **and** the
gas, electricity and water bills: they arrive together and are one decision.

A card bill payment is **not** a category. It is `kind='cc_payment'`, stored
and listed but excluded from every spending total, because the individual
swipes are the expense and the bill is settling up. A category for it would
count your month twice.

### Cards — a column, not a category

Seven values, including cash. `users.cards` is the single source of truth for
what they are (E8):

| value | type | has a bill to reconcile against |
|---|---|---|
| `amex_everyday` | credit | yes |
| `discover` | credit | yes |
| `chase_freedom` | credit | yes |
| `apple_card` | credit | yes |
| `chase_debit` | debit | no — money leaves at once |
| `sofi_debit` | debit | no |
| `cash` | cash | no, and no automatic capture at all |

Credit/debit is inferred from the names — correct it if I have any wrong. It
matters in exactly two places: a debit or cash row can never be a
`cc_payment`, and E9's reconciliation only exists for the four credit cards.

**On a `cc_payment` row, `card` is the card being *paid*, not the account
paying it.** Paying the Amex bill out of `chase_debit` is `card='amex_everyday'`.
Get this backwards and every reconciliation compares a card against someone
else's bill.

### Budgets — out of scope

Dropped at your request; metrics come later once the foundation is up. No
`users.budgets` column, no limits, no overspend line in the digest. The digest
still exists — it just reports and asks rather than judging.

Reversing this is one jsonb column and one line in the digest composer.

### Still outstanding, and what it blocks

| Needed | Blocks | Why it cannot be guessed |
|---|---|---|
| For each credit card: the strings its emails use, and the statement close day | E9 only | Printed on your statements and nowhere else. Everything except reconciliation works without it |

---

## 1. Decisions

E1–E9, referenced by number rather than re-argued, same as D1–D11.

### E1 — When extraction runs · SETTLED, and the question dissolved ✅

Originally deferred as extract-on-arrival versus park-and-batch at digest time,
trading a model call per alert against holding raw alert text in the database
for up to a day — which evaluation_plan.md §4 forbids.

**Both horns were consequences of assuming extraction is a model call.** It is
not. Phase 1b established that these emails are a fixed family of templates a
regex reads exactly (PLAN-GMAIL.md G3), and Lane C in
`capabilities/gmail_parse.py` does the same for swipe alerts.

So: **extraction runs on arrival, deterministically.** Nothing raw is ever
stored, nothing is spent, and there is no arrival queue to batch. The row is
written the moment the poll sees it, with `status='pending'` carrying the
question rather than a parked email carrying it.

*What it costs:* an alert whose wording the patterns do not cover produces
`unparsed_spend` in `email_events` and no row — you add that one by hand. That
is a pattern to add, not a token to spend, and the pile is queryable:

```sql
select subject, sender, count(*) from email_events
 where outcome like 'unparsed%' group by 1,2 order by 3 desc;
```

### E2 — The email is the entry. You are the detail. ✅

Two directions, two different rules, because the timing is genuinely asymmetric:
a bank email lands within about an hour of the spend, and you might mention the
same spend at any hour of any following day.

**Email arrives, nothing from you yet** — the common case. The row is created
with `status='pending'`: amount, card and date are known and **count
immediately**; what it was for is blank. At 21:00 the digest asks:

```
$50.00 · AMZN MKTP · amex_everyday · what was this?
```

Whatever you answer becomes the final entry — category, merchant meaning,
split, notes — and the row flips to `confirmed`.

**You mentioned it first, then the email arrives** — when an email-extracted
transaction matches a chat row of the **same amount created in the last 60
minutes**, the email is discarded and never becomes a row. You already told it;
the email adds nothing. This is the only place a time window is used, and it is
the one place where an hour is actually the right number.

**You mention it after the email, before the digest** — you said chat entries
come at any hour, so this happens. It is not in your rule, and without it the
two directions above leave a duplicate: a pending $50 row from 2pm and a chat
$50 row from 8pm, both surviving to the digest. So the rule completes
symmetrically: a chat entry that matches a **pending row of the same amount on
the same day** attaches to it — filling in the detail and confirming it —
rather than inserting a second row. If two pending rows match the amount, it
attaches to neither and the digest asks; guessing between two real $50 charges
is worse than asking once.

The net effect is that every spend has exactly one row, the amount always comes
from the bank where a bank saw it, and the meaning always comes from you.

*Reverse cost:* low, and contained — all three rules live in one function.

### E3 — The digest is composed in code. No model touches your numbers. ✅

Listing rows and quoting amounts is not a judgement call, and A6's reasoning
applies unchanged: a model should not do subtraction on your money. Composing
in code makes the digest free, instant and incapable of misquoting an amount.

The model is involved only when you *reply* to it — and answering "what was
this?" is exactly the kind of thing it should handle.

### E4 — One message a day. ✅

No immediate alerts of any kind. The nag guard in `planning.py` exists because
an assistant that interrupts twice is one you stop reading. With budgets out of
scope the digest's job is to report the day and ask about anything pending.

### E5 — Two sources: bank emails and you. No SMS. ✅

SMS would need a third-party forwarder app holding read access to every message
you receive, OTPs included, to buy latency that a 21:00 digest does not need.

**What it costs:** anything your bank only texts about is never captured
automatically, and capture lags by up to an hour. Turn on email alerts for
every card and account before Step 3 — this path is only as complete as those
settings are. Cash is fully manual by definition.

**What it saves:** no route in `app.py`, no shared secret, and Step 4 collapses
into the Gmail capability Phase 1b was already going to build.

### E6 — Splits and settlements are chat-only. ✅

A bank email cannot know a dinner was split. The split is something you say,
either up front or against a row the email already created. Buttons cannot
carry a number (D11 caps a payload at 64 bytes and a label at 20 chars), so
there is no button for this and there should not be.

### E7 — `kind` is the highest-risk field in the system. ✅

Misreading a card bill payment as a spend double-counts your entire month.
Nothing else here can be wrong by that much, and it is silent while it happens.
It gets a labelled set of its own before the capability is trusted.

### E8 — Cards are config, not a ledger. No totals are stored.

`users.cards`, one jsonb column:

```json
{"amex_everyday": {"type": "credit", "match": ["American Express", "AMEX"], "closes": null},
 "discover":      {"type": "credit", "match": ["Discover"],                 "closes": null},
 "chase_freedom": {"type": "credit", "match": ["Chase Freedom"],            "closes": null},
 "apple_card":    {"type": "credit", "match": ["Apple Card", "Goldman"],    "closes": null},
 "chase_debit":   {"type": "debit",  "match": ["Chase", "debit"],           "closes": null},
 "sofi_debit":    {"type": "debit",  "match": ["SoFi"],                     "closes": null},
 "cash":          {"type": "cash",   "match": [],                           "closes": null}}
```

Three facts per card, none of them computable: the strings its emails are
matched on, whether it is credit, debit or cash, and the day its statement
closes. `closes` stays null until you fill it; only E9 reads it.

**No stored aggregate, per card or otherwise.** A running total has to be
updated on insert, edit, merge, refund-match and delete — five write paths,
five chances to drift, and a drifted total looks exactly as authoritative as a
correct one. `sum(amount) where card = ?` is one index scan and cannot be
stale. Deriving it is what keeps it accurate; storing it is what makes it wrong.

`expenses.card` is plain text validated against the keys of this object in the
one function that writes an expense — not a CHECK constraint, so that adding a
card is a config edit rather than a migration. An unknown value is rejected
there rather than stored: `card='amex'` when the config says `amex_everyday`
would make reconciliation return a confident zero.

*Reverse cost:* low. Promote to a real table the first time a card needs its
own history — a limit that changed, a card closed with a balance.

### E9 — The card bill is a bound on what you missed, not an exact match.

You asked whether the bill matches the captured total exactly. **It does not,
and the difference is not all missed capture.** Five systematic gaps, none of
them bugs:

| Gap | Direction |
|---|---|
| Posting lag — a swipe on the 7th can post on the 9th, so the cycle boundary always cuts mid-transaction | either |
| Authorisation vs settlement — restaurant tips and gas-pump holds settle at a different amount than the alert fired for | either |
| Fees the bank charges and never emails an alert for: interest, annual fee, late fee, foreign transaction | bill is higher |
| Refunds and credits landing inside the cycle | bill is lower |
| Declined or reversed authorisations that alerted and then vanished | capture is higher |

So the delta is a **signal, not a number**. What it is good for:

```sql
-- captured, for one credit card, over its statement cycle
select sum(amount) from expenses
 where user_id = ? and card = 'amex_everyday' and kind = 'spend'
   and spent_at >= <previous close> and spent_at < <this close>;

-- what the bill actually was
select amount from expenses
 where user_id = ? and card = 'amex_everyday' and kind = 'cc_payment'
   and spent_at >= <this close>;
```

A small, stable delta month over month is the noise above. A delta that is
large or growing means bank alerts are not firing for some transactions — check
the alert settings before suspecting the model.

This is still worth having, because it is the **only** outside check that
exists here. Every other error is invisible by construction: a transaction that
never arrived leaves no row, so no query can count it. But it applies only to
the four credit cards. Debit spending has no bill, and cash has no source at
all beyond you remembering.

---

## 2. The flow

```
Gmail poll ─► /cron/tick ─► extract() ─► kind ─┬─ cc_payment → card = the card BEING PAID
                            own model call     ├─ transfer   → stored, excluded from totals
                            own context        ├─ refund     → exact-value match
                                               └─ spend
                                                     │
                                    ┌────────────────┴─ chat row, same amount, last 60 min?
                                    │                        yes → discard the email (E2)
                                    ▼
                          insert status='pending'
                          amount + card + date count immediately
                          category and meaning are blank
                                    │
you, in chat ─► log_expense ────────┤   matches a pending row, same amount, same day?
                you supply meaning  │      yes → attach and confirm it
                                    │      no  → new row, status='confirmed'
                                    ▼
                    21:00 local ─► digest, composed in code (E3)
                                     today's confirmed rows, one line each
                                     every pending row: "$50 · AMZN · what was this?"
                                     outstanding owed, only when non-zero
                                     [All good] [Fix one] [Show all]
```

---

## 3. Build order

**All five steps have shipped.** Checks live in `test_phase1.py` (43, offline),
alongside `test_gmail.py` for the email rules. What follows is what was built,
with the two places the implementation departed from this plan marked.

### Step 1 — Schema live ✅

```
migrations/003_expenses.sql      # evaluation_plan.md §10.3, shipped with Phase 1b
core/db.py                       # the reads
```

```python
expenses_between(user_id, start, end)       # the digest and the breakdown
outstanding_owed(user_id)                   # sum(owed_amount) where > 0
pending_expenses(user_id)                   # what the digest asks about
recent_chat_expense(user_id, amount)        # E2, email direction (60 min)
pending_match(user_id, amount, on)          # E2, chat direction (same day)
```

**Departure:** `find_match(user_id, amount, source, on)` became the two named
functions above. One function with a `source` flag that means "search the
opposite source" reads backwards at both call sites, and the two directions
return different shapes anyway — one row or none, versus a list whose *length*
decides the behaviour. Both are still index scans on `expenses_month_idx`.

### Step 2 — The chat path, end to end ✅

```
capabilities/expenses.py
```

Six tools, not five: `list_applications` lives here too, because Phase 1b
writes the `applications` table and nothing could read it back in chat. It is
four lines against `db.applications()` and did not justify a seventh capability
file.

1. The tools:
   ```python
   log_expense(amount, merchant=None, category=None, card=None, spent_at=None,
               headcount=1, kind="spend", notes=None)
   list_expenses(scope)          # today | week | month | owed | pending | category:<name>
   update_expense(expense_id, **fields)      # recomputes share and owed
   settle_split(expense_id, amount)          # subtracts from owed_amount
   card_summary(card, period="statement")    # E9, credit cards only
   ```
   The eleven categories go verbatim into the docstrings and into a CHECK
   constraint. The seven card values go in the docstrings and are validated
   against `users.cards` at write time (E8).
2. `log_expense` runs E2's attach rule before inserting: a pending row with the
   same amount on the same day is filled in and confirmed rather than
   duplicated.
3. The split arithmetic lives in **one** function, called by `log_expense` and
   `update_expense` both:
   ```python
   share = round(amount / headcount, 2)
   owed  = round(amount - share, 2)
   ```
   Required behaviour: *"spent 56 on dinner, between 4, amex"* stores
   `amount=56.00, share_amount=14.00, owed_amount=42.00, headcount=4,
   card='amex_everyday'`. A later `settle_split(14)` leaves `owed_amount=28.00`
   and **`share_amount` untouched** — your monthly spend must not move when a
   friend pays you back. That is the whole reason there are three columns.
4. `update_expense` recomputes share and owed whenever `amount` or `headcount`
   changes, and never on a settlement. Settlement is `settle_split`, and it
   touches `owed_amount` alone.

### Step 3 — Extraction ✅

**Departure, and the largest one in this plan.** E1 resolved to deterministic
rules, so there is no `extract()` and no model call. The work lives in
`capabilities/gmail_parse.py` as Lane C — `is_spend()` and `parse_spend()` —
with the write path in `capabilities/gmail.py:_write_spend`.

Everything the numbered points below required still holds; only the mechanism
changed. The cost line in §5 is now zero, and the thing to watch is
`unparsed_spend` in `email_events` rather than calls per alert.

1. Extracts `{amount, merchant, spent_on, card}` — **no category**. Per E6 the
   meaning comes from you at the digest, and a guessed category on a pending
   row would look like an answer rather than a question.
2. `card` is resolved by matching the email against `users.cards[*].match`. No
   match means `card=null` and the digest asks — better than a wrong card,
   which silently corrupts E9.
3. `source_ref` is the Gmail message id. Its unique partial index makes "the
   same email never produces two rows" a database guarantee rather than code
   that has to remember. Chat rows have no `source_ref`, which is why the index
   is partial.
4. One audit_log row per poll (`trigger='gmail'`, `path='rules'`, `model` null)
   rather than one per email, with a `classify` and a `dedupe` step per message
   as evaluation_plan.md §10.4 requires. A discarded email (E2, chat-first)
   logs the discard and lands as `outcome='spend_discarded'` in `email_events`
   — an email that vanishes without a record is indistinguishable from one that
   never arrived.
5. The raw email body is never written anywhere. `source_ref` and extracted
   fields only (§4), asserted by a test that greps every DB write in
   `capabilities/gmail.py`.

### Step 4 — Ingestion · ~2 hours

```
capabilities/gmail.py    # shared with Phase 1b — this is its expense half
```

No new route and no new secret (E5). Expense ingestion is a sender filter over
the Gmail poll that rides the existing hourly tick. No Pub/Sub, no second
webhook, no GCP topic — latency a 21:00 digest does not need.

**Phase 1b has landed — see [PLAN-GMAIL.md](PLAN-GMAIL.md).** The poll, the
`historyId`-free cursor decision (G1), both dedupe layers (G2) and the
`email_events` ledger already exist in `capabilities/gmail.py`. It also ships
the **card bill payment** half of this plan: a payment confirmation writes an
`expenses` row with `kind='cc_payment'`, resolving the card through
`users.cards[*].match` exactly as E8 specifies. `migrations/003_expenses.sql`
is live because that write needed somewhere to go.

What remains for this step is the **spend** half — the "you spent $X at Y"
swipe alerts — and it is still blocked on E1. When E1 is decided it is one more
branch in `gmail_parse.route()` and one more write path; the poll, the dedupe
and the event ledger are not rebuilt.

### Step 5 — The digest ✅

`capabilities/expenses.py:digest()`, fired from `/cron/tick` at
`users.digest_hour` (default 21), guarded by `users.last_digest_on` the same
way the nag is guarded by `last_nudge_on` — the tick is hourly and a digest
that fires twice is one you stop reading.

1. Composed in code (E3): today's confirmed rows one line each, then every
   pending row as a question with the ones older than three days counted, then
   the outstanding-owed line only when non-zero.
2. **Departure: no buttons.** `All good` / `Fix one` / `Show all` would each
   need a branch in `_fast_path`, and none of them can carry an answer — "Fix
   one" tells the assistant nothing it did not already know. Typing "the amazon
   one was groceries" is both shorter and strictly more expressive, and it
   already works. Add them if answering by text turns out to be the friction.
3. Answering a pending row is an ordinary chat turn. The digest names the row;
   your reply routes through `update_expense`, which flips it to `confirmed`.
   The digest is saved as an assistant message so that reply has something to
   refer back to.
4. No digest on a day with nothing confirmed and nothing pending. An assistant
   that messages you to say nothing happened is one you mute.
5. Every number is `sum()`-derived from rows; the composer makes no model call.

---

## 4. Three things that will bite

**Merchant strings are garbage.** `POS 4321 AMZN MKTP IN` is what a bank alert
contains. This matters less here than in most designs, because E6 means you
supply the meaning anyway — but it is why the digest shows the raw string
rather than a cleaned-up guess. Do not build a merchant→category memory yet;
build it the second time you answer the same merchant identically. That is the
trigger, and it is a real event rather than a guess.

**A pending row that is never answered.** It counts toward your totals with no
category forever, and it will not nag you twice (E4). Show pending rows older
than three days at the top of the digest. Do not auto-confirm them — a silently
categorised row is worse than a visibly incomplete one.

**`spent_at` comes from the alert, not from `now()`.** An email polled at 11:00
for a transaction at 09:40 belongs to 09:40. Getting this wrong puts spending
in the wrong day and, four times a year, the wrong month. Same rule as
`local_today`: one helper, everything through it.

---

## 5. Cost

**Zero.** E1 resolved to rules, so extraction makes no model call, and the
digest never did (E3). The whole capture-and-report path — poll, extract,
classify, store, digest — costs nothing per email and nothing per day.

The number to watch is therefore not calls but coverage: `unparsed_spend` and
`unparsed_payment` rows in `email_events` are alerts whose wording the patterns
miss. A rising count there is the signal that a pattern needs adding, and it is
the same query given under E1.

Tokens are spent only when you *reply* to the digest, which is one short chat
turn on the days you answer one.

---

## 6. Not in v1

| Skipped | Add it when |
|---|---|
| Budgets and overspend alerts | You have the foundation up and know which metrics you want. One jsonb column and one line in the composer |
| Receipt photos | You said no. Revisit if cash turns out to be a real gap — it is the one source with no outside check at all |
| Merchant→category memory | You answer the same merchant identically twice (§4) |
| A `people` table for who owes you | "Who owes me" needs a name attached to it; `notes` covers it until then |
| Multi-currency | You budget in a second currency. USD-only was your answer |
| SMS ingestion (E5) | A month of digests shows things your bank texts but never emails |
| Recurring / subscription detection | It is a query over rows you will not have for two months. It was on your list — it just cannot be built before the data exists |
