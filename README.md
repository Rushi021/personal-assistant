# Personal assistant — V1 prototype

Phase 1a of [PLAN-V1.md](PLAN-V1.md): tasks, the rollover ritual, the 11am
check-in. Decisions live in [ARCHITECTURE-V1.md](ARCHITECTURE-V1.md).

Everything is built. **The database is not** — that is the one step you said
you'd do by hand.

```
app.py                      FastAPI: /webhook/telegram · /cron/tick · /health
core/turn.py                handle_turn() — the single entry point
core/registry.py            capability auto-discovery (D3)
core/db.py                  supabase client, five reads, one definition of "today"
core/ctx.py                 per-turn user + outbound card buffer
core/stt.py                 Groq whisper, voice notes only (D1)
core/audit.py               one audit_log row per turn — evaluation_plan.md
core/channels/base.py       the Channel contract (D11)
core/channels/telegram.py   the one implementation
capabilities/tasks.py       six tools
capabilities/planning.py    day_status() + the rules that live in the prompt
capabilities/expenses.py    log/split/settle, analytics, the 21:00 digest
capabilities/gmail.py       the hourly poll, three lanes' writes — PLAN-GMAIL.md
capabilities/gmail_parse.py the email rules: router, payment, spend, application
test_flow.py                the checks, written per-step
test_gmail.py               the email rules, offline — no network, no DB
test_phase1.py              recurrence, expenses, digest, and one end-to-end pass
evaluation_plan.md          what is logged, why, and the failure -> fix loop
```

## 1. Database — your step

Open the Supabase SQL editor and run [migrations/001_init.sql](migrations/001_init.sql)
after editing the last statement, then [migrations/002_audit_log.sql](migrations/002_audit_log.sql)
as-is:

- `channel_user_id` — your Telegram chat id, from `@userinfobot`
- `timezone` — **your real IANA zone**, e.g. `Asia/Kolkata`. Every notion of
  "today" comes from this column. Leaving it `UTC` silently breaks the rollover
  for a few hours each night and works fine the rest of the time.

Then [003_expenses.sql](migrations/003_expenses.sql),
[004_email.sql](migrations/004_email.sql) and
[005_recurring.sql](migrations/005_recurring.sql) for expenses, the Gmail poll
and recurring reminders. Edit `users.cards` afterwards so the match strings are
what your banks actually write — that object is the only thing that maps a bank
email to one of your cards ([PLAN-EXPENSES.md](PLAN-EXPENSES.md) E8). Gmail
setup is in [PLAN-GMAIL.md](PLAN-GMAIL.md) §4.

Then verify the index the morning query depends on:

```sql
explain select * from tasks
 where user_id = '<your uuid>' and status in ('todo','in progress')
   and planned_on <= current_date;
-- wants: Index Scan using tasks_open_planned_idx   (not Seq Scan)
```

Optionally insert a second `users` row with a throwaway `channel_user_id` — the
live tests delete everything belonging to the row they run against.

## 2. Secrets

Copy `.env.example`. `SUPABASE_SERVICE_KEY` is the **service_role** key: it
bypasses RLS by design (A5), so it never goes near a browser.

```bash
fly secrets set TELEGRAM_BOT_TOKEN=... TELEGRAM_WEBHOOK_SECRET=... \
  MISTRAL_API_KEY=... GROQ_API_KEY=... \
  SUPABASE_URL=... SUPABASE_SERVICE_KEY=... CRON_SECRET=...
```

## 3. Deploy, then point Telegram at it

```bash
fly launch --no-deploy && fly deploy
curl "https://api.telegram.org/bot$TOKEN/setWebhook" \
  -d url=https://<app>.fly.dev/webhook/telegram \
  -d secret_token=$TELEGRAM_WEBHOOK_SECRET
curl "https://api.telegram.org/bot$TOKEN/getWebhookInfo"
# wants: your url, pending_update_count 0, no last_error_message
```

Then the check-in, once (see the comment at the bottom of `fly.toml`):

```bash
fly machine run . --schedule hourly --command \
  "curl -fsS -X POST https://<app>.fly.dev/cron/tick -H 'x-cron-secret: $CRON_SECRET'"
```

Hourly on purpose — the tick converts to each user's local time and only fires
in their 11am hour, for anyone who hasn't already opened the day.

## 4. Run it locally

```bash
uv venv --python 3.12 .venv && uv pip install -r requirements.txt
.venv/bin/python test_flow.py                      # structural checks, no creds
set -a && . ./.env && set +a
.venv/bin/uvicorn app:app --reload --port 8080
```

Live tests delete the test user's rows, so they need the opt-in:

```bash
TEST_CHAT_ID=<throwaway chat id> ALLOW_DESTRUCTIVE_TESTS=1 .venv/bin/python test_flow.py
```

## 5. The checks that matter

The ones you should personally watch on day one, because each fails silently:

- *"add: pay the electricity bill friday, it's quick"* → a row with `due_on`
  Friday, `estimate_min` ≤ 10, `category` `money`, none of it named by you.
- Tap **Done** → replies in under a second, and that turn's `messages.meta`
  contains no `model` key. Zero tokens is the point of the fast path.
- Second turn onward → tokens are being consumed correctly (Mistral does not
  support prompt caching, so `cache_read_tokens` will always be 0).
- A week of `meta.cost_usd` summed → compare against expected Mistral costs.

## Known ceilings

- **Model routing is by path, not intent** (D7): the 11am check-in uses
  `mistral-small-latest`, every chat turn uses `mistral-small-latest`. If
  chat-side judgement disappoints, promote `CHAT_MODEL` in `core/turn.py` — one line.
- **Blocking calls run in a thread** (`asyncio.to_thread`). Correct and simple
  at one user; revisit if this ever serves many.
- **The 5-minute question drop** (`meta.pending_question`) is specced in the
  plan and not built. It needs a real misjudgement to design against.
- **`python-dateutil` was dropped** from the plan's dependency list — `zoneinfo`
  and `date.fromisoformat` cover everything it was there for.
