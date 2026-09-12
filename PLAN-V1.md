# Implementation Plan — V1

Companion to [ARCHITECTURE-V1.md](ARCHITECTURE-V1.md). That document holds the *why*; this one holds the order of work and how each step is proved. Decisions are referenced by number (D1–D9) rather than re-argued.

**Scope:** Phase 1a — tasks, the rollover ritual, and the 11am check-in. Gmail and the job-application spreadsheet are Phase 1b, sketched at the end so nothing here has to be undone for them.

**Written under** [.claude/skills/karpathy-guidelines](.claude/skills/karpathy-guidelines/SKILL.md): assumptions stated not assumed, minimum code that solves the problem, and every step carrying a check that can fail.

---

## 0. Assumptions

Three are my call, not yours. If any is wrong, say so before Step 1 — after Step 3 they get expensive.

| # | Assumption | Why | Cost to reverse |
|---|-----------|-----|-----------------|
| A1 | **Telegram, not WhatsApp, for V1** | Your 11am check-in fires when you *haven't* messaged — outside WhatsApp's 24h window that needs a pre-approved template, which can't carry a composed briefing | Low — one adapter file behind the D11 Protocol, ~1 day. The 11am briefing would become a fixed-shape template; nothing else changes |
| A2 | Python 3.12, FastAPI, Supabase, Anthropic SDK Tool Runner | D2, D7 | High after Step 2 |
| A3 | Deploy to Fly.io on day one, not at the end | Hosting surprises found in week 3 are the expensive kind | Low |
| A4 | Weekly budget resets **Monday 00:00 local** | "This week" means Mon–Sun to most people | One constant |
| A5 | Bot server uses the Supabase `service_role` key and bypasses RLS | D5 | n/a |
| A6 | **Budget arithmetic is never spoken aloud** | Your call. It decides *when* to speak, it is not itself something to report | One prompt line |

---

## 1. Stack

| Layer | Choice | Note |
|-------|--------|------|
| Language | Python 3.12 | |
| Web | FastAPI + uvicorn | Webhook and cron endpoints only |
| DB | Supabase Postgres via `supabase-py` | Free tier; the 7-day inactivity pause is moot with a daily cron |
| Model loop | `anthropic` SDK — `client.beta.messages.tool_runner` + `@beta_tool` | D2. The SDK owns the agent loop |
| Models | `claude-haiku-4-5` conversational · `claude-opus-5` planning | D7 |
| Transcription | Groq `whisper-large-v3-turbo` | D1. Only when the channel sends no transcript |
| Host | Fly.io, 1× shared-cpu-1x 256MB | ~$5/mo |
| Scheduler | Fly cron → `POST /cron/tick` | D6 |

**Dependencies, complete:** `fastapi uvicorn anthropic supabase httpx python-dateutil`. Nothing else is added without a reason written down.

---

## 2. Build order

Each step states its goal, the files it touches, and numbered actions with a **verify** that can actually fail. Tests live *in* the step that creates the behaviour, not in a hardening pass at the end — a check written three days later tests what you remember, not what you built.

Estimates assume Python fluency and unfamiliarity with these APIs.

### Step 0 — A deployed bot that echoes · ~half a day

**Goal:** prove the plumbing before any logic exists to obscure a failure.

```
app.py                     # POST /webhook/telegram, GET /health
core/channels/base.py      # Inbound, Button, Channel Protocol — D11
core/channels/telegram.py  # the one implementation
fly.toml · Dockerfile · requirements.txt
```

Write `base.py` first, then implement against it. Forty lines now; a rewrite later (D11).

1. BotFather → token → `fly secrets set` → **verify:** `fly secrets list` shows it; the token is not in `git log -p`.
2. Deploy, register the webhook against the Fly URL → **verify:** `getWebhookInfo` returns your URL with `pending_update_count: 0` and no `last_error_message`.
3. Echo the message verbatim → **verify:** you message the bot from your phone and it echoes — **from the deployed app, with your laptop closed.**
4. → **verify (D11):** `app.py` holds no Telegram-specific field access. Everything it touches comes off `Inbound`. The test that matters: could you write `whatsapp.py` without opening `app.py`?

### Step 1 — Schema live, rollover query indexed · ~half a day

**Goal:** the one query that runs every morning is fast on day one.

```
core/db.py
```

1. Run [migrations/001_init.sql](migrations/001_init.sql) in the Supabase SQL editor → **verify:** all three tables exist and `\d tasks` shows the three partial indexes.
2. **Set `users.timezone` to your real IANA zone** and replace the seed chat id → **verify:** `select timezone from users` is not `'UTC'` unless you actually live there. Every notion of "today" comes from this.
3. Write five queries and no more:
   ```python
   get_user_by_channel(channel, channel_user_id)
   open_tasks(user_id, planned_on_or_before=None)   # the rollover query
   unplanned_tasks(user_id)                         # the inbox
   week_minutes_used(user_id, week_start)
   recent_messages(user_id, limit=20)
   ```
   → **verify:** `EXPLAIN` on `open_tasks` shows `Index Scan using tasks_open_planned_idx`, not `Seq Scan`. This is the check that matters — it's the query that grows.

**Assertion left behind:** `test_flow.py::test_rollover_query_uses_index`.

### Step 2 — Tasks capability, end to end · ~1 day

**Goal:** the first real conversation.

```
core/registry.py   # ~15 lines — D3
core/turn.py       # handle_turn()
capabilities/tasks.py
```

1. Six tools, no more:
   ```python
   add_task(title, planned_on=None, due_on=None, deadline_hard=False,
            estimate_min=25, priority_level="normal", category=None, notes=None)
   list_tasks(scope)          # today | open | unplanned | overdue
   update_task(task_id, **fields)
   complete_task(task_id)
   drop_task(task_id)
   reschedule_task(task_id, planned_on)
   ```
   → **verify:** `len(registry.TOOLS) == 6` and every one has a docstring the model can act on.
2. `handle_turn()` does five things: load user → load context → run the Tool Runner → persist both messages → return the reply. Anything else belongs in a capability → **verify:** `core/turn.py` is under 80 lines. If it's longer, logic has leaked into it.
3. → **verify (the real one):** *"add: pay the electricity bill friday, it's quick"* creates a row with `due_on` set to Friday, `estimate_min <= 10`, and `category='money'` — **without you naming any of those fields.**

**Assertion left behind:** `test_flow.py::test_natural_language_creates_structured_task`.

### Step 3 — The fast path · ~half a day

**Goal:** D4 — check-off with no model call. The cost lever, so it lands before the clever parts.

```
core/channels/telegram.py   # inline keyboards + callback_query
app.py                      # route callbacks away from handle_turn()
```

1. Every task rendered carries `Done · Tomorrow · Drop`, task id in the callback payload → **verify:** the payload round-trips a uuid intact, and is **≤64 bytes** (D11 — a raw uuid is 36, so it fits with a short prefix).
2. **Obey the WhatsApp ceiling now** (D11): at most **3 buttons**, labels **≤20 chars**, replies in **plain text**, and **no message editing** — send a new confirmation line instead → **verify:** no `parse_mode` and no `editMessageText` anywhere in `core/channels/telegram.py`.
3. The callback handler writes to the DB and answers the callback → **verify:** tapping Done updates the row, replies in under a second, and **`messages.meta` records no model call for that turn.** That last clause is the whole point of the step.
4. → **verify (D11):** `grep -rn "telegram" core/turn.py capabilities/` returns nothing. A single hit means the channel has leaked into the logic and the swap is no longer a day's work.

**Assertion left behind:** `test_flow.py::test_fast_path_makes_no_model_call`.

### Step 4 — Planning: rollover, budget, nag · ~1.5 days

**Goal:** D8 and D9 — the step that decides whether this is an assistant or a to-do app. Slow down here.

```
capabilities/planning.py
```

Mostly a `PROMPT` plus one read-only tool, which is the point: **rules live in the prompt, arithmetic lives in code.** A model should not be doing subtraction on your week.

```python
day_status()   # today's committed work, unreconciled count from prior days,
               # tasks past slip_threshold, and one boolean: week_is_full
```

Six rules, in order of how badly it hurts to get them wrong:

1. **Capture is never blocked.** `add_task` succeeds before any reconciliation text is composed. Always, not usually → **verify:** with 10 unreconciled tasks, a bare capture still returns a confirmation *first*.
2. **Nag at most once per day.** Guarded by `users.last_nudge_on` in code, never left to the model's judgement → **verify:** five captures in a row produce exactly one nudge.
3. **Reconciliation triggers on the first *planning* act of the day** — asking what to do, or committing to today. Never on a bare capture → **verify:** an 11pm capture starts no ritual.
4. **Over 8 unreconciled → bulk triage** (Keep all / Drop all / One by one) → **verify:** 12 open tasks produce one message, not twelve.
5. **`slip_count` past `slip_threshold` → challenge once**, then leave it alone → **verify:** the challenge fires on day 4 and not again on day 5.
6. **Hard deadline onto a full week → add anyway, then alert**, naming the one soft task that could move → **verify:** the row exists before the warning text is generated.

**A6 — the budget never speaks.** `day_status()` returns `week_is_full` as a boolean; minutes and remainders stay internal. The assistant may say *"that's a full week"*; it may not say *"you have 4h 20m left"* → **verify:** grep the prompt for any instruction to report minutes. There should be none.

**Assertion left behind:** `test_flow.py::test_capture_never_blocked`, `::test_nag_fires_once`, `::test_slip_challenge_fires_once`.

### Step 5 — The 11am check-in · ~half a day

**Goal:** D6 — proactive, using the same entry point as the webhook.

```
app.py     # POST /cron/tick, shared-secret header
fly.toml   # schedule
```

1. The tick loops users, converts to local time, and for anyone at 11:00 who has sent nothing today calls `handle_turn(user_id, "<system> morning check-in")` → **verify:** it is the *same function* the webhook calls. If you wrote a second path, the seam is gone.
2. → **verify:** fires once at 11am local, and does **not** fire on a day you messaged at 9am.

**Assertion left behind:** `test_flow.py::test_checkin_skipped_when_already_active`.

### Step 6 — Make failure safe · ~half a day

**Goal:** the failure modes that lose data rather than crash.

1. **Idempotency** — insert into `messages` with `channel_msg_id` *first*; a unique violation means a retry, so return 200 and stop → **verify:** replay the same Telegram update twice; exactly one task is created.
2. **Never 500 at a webhook.** Always return 200; a non-200 makes Telegram retry and duplicate the work → **verify:** force an exception mid-turn; the webhook still returns 200 and you get a plain failure reply.
3. **Cost ledger** — write `model`, `input_tokens`, `output_tokens`, `cost_usd` into `messages.meta` every turn → **verify:** a week of `meta` sums to a real monthly projection you can compare against the $13 estimate.
4. **Prompt caching on** → **verify:** `cache_read_input_tokens > 0` on the second turn. Zero means something volatile sits in the cached prefix (D7).

---

## 3. Three things that will bite

Each is a silent failure, not a crash — which is why each gets a named check.

**Timezone.** Store `timestamptz` (UTC) always; compute "today" and "this week" in the user's zone. Every date comparison goes through one helper, `local_today(user)`, and nothing calls `date.today()` directly → **verify:** `grep -rn "date.today()" core/ capabilities/` returns nothing.

**The 5-minute question drop.** When it asks for a missing deadline, write `meta.pending_question = {field, asked_at}` on the task. The next turn answers it or, past 5 minutes, clears it silently → **verify:** answer at 6 minutes; the task stays undated and no chase happens.

**The weekly boundary.** `week_minutes_used` sums from Monday 00:00 *local*, not `now() - 7 days` → **verify:** a task planned Sunday and one planned Monday fall in different weeks.

---

## 4. Simplicity pass — candidates to cut

Applying §2 of the guidelines to my own draft. Each of these is speculative; I'd cut all three, but they're in the SQL you may already have run, so they're your call rather than a silent edit.

| Thing | Why it's speculative | Recommendation |
|---|---|---|
| `users.checkin_hour` | Config for a value that never changes — it's 11, and there's one user | Hardcode 11; restore the column if a second user ever disagrees |
| `source` CHECK listing `'sheets'`, `'import'` | Constrains against capabilities that do not exist | Trim to `chat · voice`; widen when Phase 1b lands |
| `week_summary()` tool | I specced it, then couldn't name a question `day_status()` doesn't answer | Cut — already removed from Step 4 above |

Kept despite looking speculative, with reasons: **`auth_id` + RLS** (you asked for multi-user and a web app; ten lines now, a data migration later) and **`meta jsonb`** (it is the mechanism for *avoiding* speculative columns, not an instance of one).

---

## 5. Doesn't block the build

| Open | Default in use | Revisit |
|------|---------------|---------|
| Where your "real deadline" line sits | The model judges; you correct it in passing | The first time it misjudges — that's the example you need |
| What it should never do without asking | Nothing destructive except `drop_task`, which always confirms | When something concrete comes to mind |
| Per-day budget override | Not built. Needs a `users.meta` column | When you actually want to override a day |
| WhatsApp instead of Telegram | A1 | Once the check-in has earned a template |
| Web app | Chat only | When bulk triage in chat gets tedious |

---

## 6. Phase 1b — sketch

Listed so Step 4 doesn't paint it into a corner. Each is one file against the D3 contract; core is untouched.

```
capabilities/gmail.py    # deadline + application-status extraction
capabilities/sheets.py   # job-application spreadsheet status column
```

- **Polling beats push.** `history.list` every 15 minutes on the existing cron tick. Gmail Pub/Sub needs a GCP topic, a subscription and a second webhook, for latency you don't need.
- Extraction runs as **its own Claude call with its own context** — separate prompt, separate window, `claude-opus-5` because a missed bill is the failure case. Not a service, not A2A: a function that makes a call.
- Findings become ordinary tasks with `source='gmail'`, `source_ref=<message id>`. No second table, no sync engine.
- `source_ref` is the dedupe key → the same email must never produce two tasks.

---

## 7. Total

**~5 days of focused work** to something you use daily.

Ordering rationale in one line: riskiest infrastructure first (Step 0), the free interaction path before the expensive one (Step 3 before Step 4), and the thing you'll actually judge it on last — when everything beneath it is already proven.
