# Evaluation Plan — what to log, why, and how failures become fixes

Companion to [PLAN-V1.md](PLAN-V1.md) and [ARCHITECTURE-V1.md](ARCHITECTURE-V1.md).
That one holds the *why* of the build; this one holds **the why of every field
you write down about a turn**, and the loop that turns a bad reply into a test
that fails.

Scope of what exists today: `capabilities/tasks.py` (six tools),
`capabilities/planning.py` (`day_status`), the button fast path in `app.py`, the
cron check-in. Email and expenses get their own sections at the end, marked as
placeholders until their workflows are decided.

---

## 0. The one claim this document rests on

You cannot evaluate what you cannot replay.

Right now a turn leaves behind exactly two rows in `messages` (yours and the
assistant's) and a token tally in `meta`. That tells you *what it said*. It does
not tell you **what it called, what came back, what the arithmetic was, or why
it chose that sentence** — which is the only thing worth knowing when the reply
is wrong.

So: one new table, one row per turn, written in a `finally` block. Everything
below is a column or an entry in its `steps` array.

**Why a table and not just `messages.meta`:** a turn that throws never reaches
`db.save_message(... "assistant" ...)`. The turns you most need to audit are
exactly the ones that would leave no row at all. A `finally` write to its own
table is the only shape that captures a crash.

---

## 1. The audit_log table

The schema is [migrations/002_audit_log.sql](migrations/002_audit_log.sql) —
run it after `001_init.sql`. Columns are explained one by one in §2; the four
indexes exist because §5's invariant queries are containment lookups into
`steps`, and a GIN index is the difference between those being instant and
being a scan of every turn you have ever taken.


**Where it is wired** — the trace opens and closes at the app boundary, so
nothing below it has to remember to close one:

| File | What it does |
|---|---|
| [core/audit.py](core/audit.py) | `begin` / `step` / `usage` / `finish`, redaction, the cost ledger, the retention sweep |
| [app.py](app.py) | `begin()` after the user is known; `finish()` on both the success and the failure arm of `webhook()` and `/cron/tick` |
| [core/registry.py](core/registry.py) | wraps `BetaFunctionTool.call` once — every tool is traced and no capability knows the audit log exists |
| [core/turn.py](core/turn.py) | one `model` event per hop, then the token and cost tally |
| [core/ctx.py](core/ctx.py) | `card()` events |
| [capabilities/planning.py](capabilities/planning.py) | the `day_status` `decision` event |

`step()` is a no-op when no turn is in flight, so `handle_turn()` still runs
fine from a test or a script with no trace open.

---

## 2. Turn-level fields — what each one is for

| Field | Example | Why it is logged | What it catches |
|---|---|---|---|
| `trigger` | `button` | Separates the four ways a turn can start — `message`, `voice`, `button`, `cron` | The cron path failing silently while chat looks fine — different code, same function |
| `path` | `fast` | D4 claims ~40% of turns cost nothing | Fast-path share drifting down: a button broke and you are paying Haiku for check-offs |
| `model` | `claude-haiku-4-5` | Cheap turns and planning turns fail differently | Opus firing on a bare capture, or Haiku being asked to do the morning briefing |
| `prompt_hash` | `a41f0c9e2b77` | Pins a reply to the exact prompt + tool schema that produced it | "It got worse on Tuesday" → which prompt edit landed Tuesday. Without this, quality regressions are unattributable |
| `input` | `add: pay electricity friday` | The replay key | Nothing is reproducible without it |
| `reply` | `Added. Friday.` | The thing being judged | — |
| `latency_ms` | `2840` | Chat that takes 6s stops getting used | A tool doing an unindexed scan; Groq STT hanging |
| `input/output/cache_read_tokens` | `4210 / 96 / 3900` | Step 6.4 wants `cache_read > 0` on turn 2 | Something volatile crept into the cached prefix — the registry emitting tools in a new order, a timestamp in the system prompt. Silent 10x cost |
| `cost_usd` | `0.0041` | Turns the ~$13/month estimate into a measured number | A prompt change that quietly doubled the context |
| `status` + `error` | `error / KeyError: 'timezone'` | Webhooks always return 200 (Step 6.2), so failures are invisible by design | Every failure you would otherwise only hear about as "it didn't reply" |
| `verdict` / `verdict_note` | `bad / nagged me twice` | Your judgement is the only ground truth that exists | This is the column the whole eval loop reads from |

`verdict` is set by you, by hand, in the Supabase table editor, on the handful of
turns a day that felt wrong. No labelling tool. The triage index exists so the
query is `where verdict = 'bad'` and you are done.

---

## 3. `steps[]` — the reasoning trail

An ordered array of small objects. This is the "how did it reach that answer"
part. Five event types, no more:

```jsonc
// one per hop of the tool-use loop
{"t":"model","stop":"tool_use","out":96,"cache_read":3900}

// a tool was called — arguments exactly as the model supplied them
{"t":"tool","name":"add_task","ms":140,"ok":true,
 "args":{"title":"pay electricity bill","due_on":"2026-09-18","estimate_min":5,
         "category":"money","deadline_hard":true},
 "result":{"id":"7c1f…","status":"todo"}}

// a button tap wrote directly, with no tool call to carry it (fast path only)
{"t":"write","table":"tasks","op":"done","row":"7c1f…","matched":true}

// arithmetic the user is never allowed to see, recorded so YOU can see it
{"t":"decision","name":"day_status",
 "in":{"used_min":690,"budget_min":720,"last_nudge_on":"2026-09-12","stale":3},
 "out":{"week_is_full":false,"should_nudge":true,"bulk_triage_sent":false}}

// a card was queued through ctx.card()
{"t":"card","text":"pay electricity bill · 2026-09-18","buttons":["Done","Tomorrow","Drop"]}
```

### Why each event earns its place

**`t:"model"`** — `stop` and the per-hop token split are how you see the shape of
the loop. Four hops on a one-line capture means the model is groping; one hop
with 8k input tokens means history is too long. Neither is visible from the
final reply.

**`t:"tool"`, args verbatim** — the single highest-value field in the whole
schema, and the reason the wrapper lives in the registry rather than in each
capability. Almost every failure of this system is a *wrong argument*, not a wrong
sentence: `planned_on` set when the user meant `due_on`, `deadline_hard` true
for a self-imposed date, an `estimate_min` of 25 on something you said was
quick. The reply — "Added, Friday" — looks identical in all four cases. Only the
args show it.

**`t:"write"`** — the fast path writes to `tasks` directly and calls no tool, so
without this a button tap would leave an empty trail. `matched` is the field
that matters: it is the difference between "Done: gym." and "Already handled.",
and it is how a tap that silently changed nothing becomes visible.

On the model path there is no separate write event, because the tool event
already carries it — `_update()` returns `{"error": ...}` when it matches no
row, and the wrapper marks that call `ok:false` whatever the model says next.

**`t:"decision"` — the most important one for this codebase specifically.**
A6 says minutes never get spoken aloud; `day_status` hands the model one
boolean. That is right for the user and blinding for the debugger. When it says
"that's a full week" and you disagree, the log must carry `used_min=690,
budget_min=720` or you cannot tell whether the model misread a boolean or the
boolean was wrong. Same for `should_nudge` and `last_nudge_on`: the guard lives
in code, so its inputs must be in the trace to prove it held.

**`t:"card"`** — cards go out through `ctx.drain()` *after* the reply, so the
message the user actually sees is reply-plus-cards. Judging the reply text alone
judges half the output. Also the only way to check the D11 ceiling (≤3 buttons,
≤20 chars) against what really shipped, rather than against the constant in the
source.

---

## 4. What must NOT go in the log

Not laziness-eligible. This is a trust boundary.

| Never store | Store instead | Why |
|---|---|---|
| Full email bodies (Phase 1b) | `gmail_message_id`, subject, sender domain, extracted fields | Traces are a second copy of your inbox in a table with no RLS in front of it (service_role, A5). Keep the pointer, not the payload |
| Card / account numbers, UPI ids, OTPs | last 4 digits, or a salted hash | An expense tracker that reads bank alerts will otherwise log PANs and one-time codes forever |
| `ANTHROPIC_API_KEY`, `SUPABASE_SERVICE_KEY`, bot token | nothing | Redact `error` before writing: `fly logs` and a DB row are two different blast radii, and the `except` arm writes tracebacks |
| Raw voice audio | the transcript + `stt_ms` | Size, and you never listen to it |

Redaction happens **at write time**, once, in the trace writer. Not at read
time, not "we'll scrub it later".

**Retention:** delete `audit_log` rows older than 60 days where `verdict is null`. Keep
everything labelled forever — those are your eval cases. One line in the
existing `/cron/tick`.

---

## 5. The checks that run against the production audit log

`test_flow.py` already encodes the invariants. They are asserted pre-deploy
against synthetic input. The same assertions, run over real audit_log rows, are your
production evaluation — no new assertions to invent, just a different input.

Each is a SQL query over `steps`, runnable as one saved Supabase query:

| Invariant | Auto-detectable as | Why it matters |
|---|---|---|
| **Capture is never blocked** (D8, rule 1) | a trace whose input starts a capture but whose `steps` has no `tool:add_task` before the first text | The single rule you said hurts most to break |
| **Nag fires once per day** | `count(*) where steps @> '[{"t":"decision","out":{"should_nudge":true}}]'` grouped by day > 1 | The code guard is supposed to make this impossible. Logging it is how you learn the guard broke, rather than trusting it |
| **The budget never speaks** (A6) | `reply ~* '\d+\s*(min|minutes|hours|h\b|hrs)'` on any trace whose steps contain `day_status` | A prompt-only rule with nothing enforcing it. Cheapest possible regex, catches the exact leak |
| **No invented task ids** | any uuid appearing in `t:"tool"` args that was not returned by an earlier `tool` or `write` step in the same trace | The prompt forbids it; models do it anyway. Silently updates nothing, and the reply says it worked |
| **Tool claimed success, DB didn't move** | a `t:"tool"` with `ok:true` and no matching `t:"write"` | "Marked done" on a task that is still open |
| **Fast path stays free** | any trace with `path='fast'` and a non-null `model` | D4's whole economic argument |
| **Cache is warm** | `cache_read_tokens = 0` on any non-first turn of a day | A 10x cost regression with zero user-visible symptom |
| **Slip challenge fires once** | two audit_log rows, different days, both with the same task id in `decision.out.slipping` | Being nagged twice about the same task is how you stop reading the messages |

Six of these eight are impossible to check without `steps`. That is the argument
for the array.

---

## 6. The numbers to watch weekly

One saved query, seven rows, read on Sunday. Not a dashboard.

| Metric | Target | What a miss means |
|---|---|---|
| fast-path share of turns | ≥ 35% | Buttons aren't being used, or aren't being rendered — and cost is 3x what it should be |
| median / p95 latency, model path | < 2s / < 5s | Something slow got added to the loop |
| cost per day | < $0.45 | The $13/month estimate is drifting |
| tool error rate | < 1% | Usually a schema drift or an id the model invented |
| turns with `status='error'` | 0 | Every one is a reply you never got |
| `verdict='bad'` rate | trending down | If this is flat across two weeks of prompt edits, the edits aren't working |
| open tasks never touched in 14 days | < 10 | Not a system metric — a signal the assistant is failing at its actual job |

The last row matters more than the other six. Everything above it can be green
while the thing is useless.

---

## 7. Failure → fix loop

The point of all of the above.

1. **Notice.** A reply annoys you. You mark `verdict='bad'` with a five-word
   note, in the Supabase table editor, on your phone, same day. If this step
   takes more than ten seconds you will stop doing it and the loop dies.
2. **Read the trace, not the reply.** Open `steps`. In practice it is one of
   four things: wrong tool args, a tool that returned an error the model
   glossed over, a `decision` whose inputs were wrong, or the model ignoring a
   prompt rule it was given correctly.
3. **Freeze it as a case.** Append one line to `evals/cases.jsonl`:
   ```json
   {"id":"cap-023","input":"add: pay electricity friday, quick",
    "expect":[["tool","add_task"],["arg","due_on","2026-09-18"],
              ["arg","planned_on",null],["arg","estimate_min","<=10"],
              ["not_in_reply","minutes"]]}
   ```
   **Assert invariants, never exact strings.** The reply wording will change
   every time you touch the prompt; "did it set `due_on` and not `planned_on`"
   will not. A suite that breaks on rewording gets deleted within a month.
4. **Fix.** Prompt line, tool docstring, or code. Which one is decided by step 2:
   a rule the model obeyed but whose *inputs* were wrong is a code bug and no
   amount of prompt editing fixes it.
5. **Re-run the whole case file**, not just the new case. This is the only step
   that catches the fix that broke two older behaviours — the failure mode that
   makes prompt iteration feel like whack-a-mole.
6. **Promote.** A case that fails twice in different ways becomes an assertion in
   `test_flow.py`, where it runs before every deploy.

The runner is a loop over the JSONL calling `handle_turn` against a throwaway
user row and checking the resulting trace. Roughly 60 lines. No framework, no
pytest plugins, no eval platform — the trace table already *is* the result store.

**Where an LLM judge earns its place, and only there:** the 11am briefing. "Did
it pick the right three things to say" has no invariant. Everything else in this
system is checkable with an equality test, so a judge would just add cost and
non-determinism. Add it when you have twenty labelled briefings to calibrate it
against, not before.

---

## 8. Seeing a turn live

You asked for visibility while building. Cheapest to most:

1. `fly logs` with the step events also printed as one JSON line each — same
   data, zero extra infrastructure, works right now.
2. A saved Supabase query: last 20 audit_log rows, `steps` expanded. This is the one you
   will actually use.
3. *Optional, ~10 lines:* a `/why` command in Telegram that replies with the last
   trace's step list as plain text. Worth it only if you find yourself opening
   Supabase on your phone more than once a day. Build it the second time that
   happens, not the first.

No dashboard, no Grafana, no OpenTelemetry. One table and SQL covers all of it
until there is a second user.

---

## 9. Phase 1b — email tracking

Placeholder, because the workflow isn't built. What changes about logging:

- Extraction runs as **its own Claude call with its own context** (PLAN §6), so
  it needs **its own trace row**: `trigger='gmail'`, `input=<subject + sender +
  message_id>`, never the body.
- The field that matters is `source_ref` dedupe: log every extraction's
  `gmail_message_id` and whether it was skipped as already-seen. **The same email
  producing two tasks is the defining failure of this capability**, and it is
  only visible in a log that records the skip as well as the create.
- Classification is the thing being evaluated: `{is_application_update,
  is_interview_invite, is_rejection, company, role, new_status}`. Log the
  extracted object *and* the model's confidence if you ask for one — a labelled
  set of these is a genuine offline eval you can run without sending messages.
- Two error rates, tracked separately, because they are not equally bad: a
  missed interview email (silent, costly) and a false positive (noisy, cheap).
  Optimise recall; log both.

## 10. Phase 1c — expense tracking

Settled in the interview of 2026-09-13. Recorded here because the logging spec
is downstream of the workflow, and because the arithmetic below is the part
that will be wrong silently.

### 10.1 The workflow, as decided

| Question | Your answer |
|---|---|
| Sources | bank/card **emails** (rides the Gmail poll) and **direct** chat/voice. SMS dropped — see PLAN-EXPENSES.md E5 |
| Confirmation | **evening digest** — one message a day, not a card per transaction |
| Unreviewed items | the **amount** counts immediately; what it was for stays blank until you answer at the digest |
| Duplicates | asymmetric, see PLAN-EXPENSES.md E2 — an email is discarded if you said it in the last hour; a chat entry attaches to a pending row from the same day |
| Categories | **fixed**: food, groceries, transport, housing (rent + gas/electricity/water), subscriptions, shopping, health, travel, entertainment, fees, other |
| Card | seven values incl. `cash`; config in `users.cards`, no aggregates stored (E8). On a `cc_payment` row it is the card **being paid** |
| Reconciliation | credit cards only, and a bound rather than an exact match — see PLAN-EXPENSES.md E9 |
| Budget | **out of scope for now** — metrics come once the foundation is up |
| Transfers / card-bill payments | **not expenses** — count the individual swipes |
| Currency | **USD only** — no currency column, no FX, no rate source |
| Splits | you say "$56 between 4" (**headcount includes you**); it computes your share and what you're owed |
| Refunds / cashback | **exact-value match** to the original, shown in the digest as already applied, editable |
| Settling up | you say how much came back; it comes off the outstanding owed |

### 10.2 The three columns, and why there are three

A split dinner: `$56 between 4`.

```
amount        56.00   what the card was actually charged
share_amount  14.00   amount / headcount  — your cost, immutable
owed_amount   42.00   outstanding to you  — decrements as people repay, to 0
```

Your three metrics fall straight out, and each stays correct as repayments land:

```sql
-- 1. reconciles against the card statement
select sum(amount)       from expenses where kind = 'spend'  and <month>;
-- 2. "to be received"
select sum(owed_amount)  from expenses where owed_amount > 0;
-- 3. monthly expenditure — what the budget alert fires against
select sum(share_amount) - coalesce(sum(amount) filter (where kind = 'refund'), 0)
  from expenses where kind in ('spend','refund') and <month>;
```

**The trap, stated plainly.** You described expenditure as *amount minus what
you're owed*, and separately said owed should shrink as people pay you. Those
two together make your spending climb as you get repaid: `56 - 42 = 14` today,
`56 - 0 = 56` once everyone settles — the same dinner costing you four times
more in November than it did in October. Deriving expenditure from
`share_amount` instead gives $14 in both months, leaves `owed_amount` free to
decrement exactly as you described, and keeps `sum(amount)` matching your card
bill. Nothing you asked for is lost; the subtraction just moves.

`headcount` is stored too, so a mis-heard "between 4" is a one-field fix in the
digest and all three numbers recompute.

### 10.3 Schema

```sql
-- migrations/003_expenses.sql
create table if not exists expenses (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references users(id) on delete cascade,

  spent_at      timestamptz not null,
  amount        numeric(12,2) not null,             -- charged to the card
  share_amount  numeric(12,2) not null,             -- yours; = amount when headcount = 1
  owed_amount   numeric(12,2) not null default 0,   -- outstanding to you
  headcount     int not null default 1,

  merchant      text,
  kind          text not null default 'spend'
                  check (kind in ('spend','refund','transfer','cc_payment')),
  refund_of     uuid references expenses(id),

  -- Null for a transfer or a card-bill payment: those are not spending and
  -- forcing a category on them would put a made-up one in your breakdown.
  -- housing covers rent/mortgage AND the gas, electricity and water bills:
  -- they arrive together, move together, and are the same decision.
  category      text
                  check (category in ('food','groceries','transport','housing',
                                      'subscriptions','shopping','health',
                                      'travel','entertainment','fees','other')),

  -- Which card, including 'cash'. Validated against the keys of users.cards at
  -- write time rather than by a CHECK, so adding a card is a config edit (E8).
  -- On a cc_payment row this is the card BEING PAID, not the account paying.
  -- Null only when an email named no card the config recognises.
  card          text,
  source        text not null check (source in ('email','chat','voice')),
  source_ref    text,                               -- gmail message id

  -- 'pending' is an email-extracted row whose amount counts but whose meaning
  -- you have not supplied yet (E2/E6). The digest asks; your answer confirms.
  status        text not null default 'confirmed'
                  check (status in ('pending','confirmed','deleted')),
  notes         text,
  meta          jsonb not null default '{}'::jsonb,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),

  -- A CONFIRMED spend with no category is invisible in the breakdown. A pending
  -- one is allowed to have none — that is exactly what makes it pending.
  constraint confirmed_spend_needs_category
    check (status <> 'confirmed' or kind <> 'spend' or category is not null)
);

create index if not exists expenses_month_idx on expenses (user_id, spent_at desc);
create index if not exists expenses_owed_idx  on expenses (user_id) where owed_amount > 0;
create unique index if not exists expenses_dedupe_idx
  on expenses (user_id, source_ref) where source_ref is not null;

-- cards: one jsonb column, not a table (PLAN-EXPENSES.md E8). Card -> config.
-- No totals live here: a stored aggregate drifts across five write paths and
-- a drifted total looks exactly as authoritative as a correct one.
-- {"amex_everyday": {"type": "credit", "match": ["American Express"], "closes": null}}
-- Adding a card is an edit to this object, which is why expenses.card has no
-- CHECK constraint: it is validated against these keys at write time instead.
alter table users add column if not exists cards jsonb not null default '{}'::jsonb;

-- No budgets column. Out of scope until the metrics are decided.

drop trigger if exists expenses_touch on expenses;
create trigger expenses_touch before update on expenses
  for each row execute function touch_updated_at();
```

`transfer` and `cc_payment` rows are excluded from all three metrics by the
`kind` filter. They are still stored — a card-bill payment you never see is
indistinguishable from one that was silently dropped.

No `currency` column, no `paid_by` / person table, no settlement ledger, and
no `merged_from`: under E2 nothing is ever merged. An email either is discarded
before becoming a row or it becomes the row, and a chat entry either fills in a
pending row or is its own. The audit log records which happened; a column
restating it would only be a second place to disagree.
Add a person table the first time "who owes me" needs a name attached to it;
today `notes` holds that for free.

### 10.4 What to log, on top of §2 and §3

Every extraction is a model call, so it gets an ordinary trace row
(`trigger='email' | 'chat'`). Four extra things, all of which exist
because they are the ways this capability fails **silently**:

| Log | Why | What it catches |
|---|---|---|
| `{"t":"dedupe","decision":"discarded"\|"attached"\|"new","against":"<id>","gap_s":41,"amount":56.00}` | E2 drops emails and attaches chat entries to pending rows. Both make a transaction disappear from where you expected it | A discarded email that should have been a row is indistinguishable from one that never arrived — unless the discard itself is logged |
| `{"t":"classify","kind":"cc_payment","card":"amex_everyday","confidence":"low","merchant_raw":"..."}` | Misreading a card-bill payment as a spend **double-counts your entire month**, and a wrong `card` silently corrupts E9 | The single largest possible error in this system. Log `kind` and `card` on every extraction, right or wrong |
| `{"t":"split","said":"between 4","headcount":4,"amount":56,"share":14,"owed":42}` | The arithmetic is done in code from a number the model read out of your sentence | "between 4" heard as "between 40". The inputs and outputs of the division, both, or you cannot tell which half was wrong |
| `{"t":"refund_match","matched":"<id>","amount":56.00,"exact":true}` | Exact-value matching picks the *wrong* original when you buy the same thing twice | A refund cancelling a purchase you didn't return |

Two rates worth watching weekly, separately, because they are not equally bad:
**missed transactions** (silent, corrupts every total) and **wrong category**
(visible, one tap in the digest). Optimise for never missing one.

Per §4, none of this stores an account number: `source_ref` is a Gmail message
id, and the raw email body is never written.

## 11. Deliberately not doing

| Skipped | Add it when |
|---|---|
| OpenTelemetry / spans / a tracing vendor | There is a second service to correlate across. There is one process |
| A separate `tool_calls` table | `steps` jsonb with a GIN index stops answering a question you actually have |
| A labelling UI | Marking a verdict in the Supabase table editor becomes the thing you skip |
| Automatic regression alerts | You have two weeks of baseline to alert against |
| An LLM judge on ordinary turns | Invariants stop covering a failure. §7 |
| Logging the full system prompt per turn | Never — `prompt_hash` plus git history is the same information for 12 bytes |
| A `write` event for tool-driven writes | Never — the tool event carries args, result and `ok`. A second event would restate it |
| Capping the size of `steps` | A `list_tasks` over a few hundred rows makes a row big enough to notice |
