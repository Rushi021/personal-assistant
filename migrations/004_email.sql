-- ============================================================================
-- 004_email.sql  ·  Phase 1b — Gmail ingest
--
-- Two tables. email_events is the dedupe ledger AND the outcome log, because
-- they are the same fact: "we have already dealt with this message id, and
-- here is what we decided". Splitting them would mean two places to disagree
-- about whether an email was handled.
--
-- Nothing here stores a body. evaluation_plan.md §4 is a trust boundary:
-- sender, subject and extracted scalars only.
--
-- Run in the Supabase SQL editor after 003_expenses.sql. Safe to re-run.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- email_events
--
-- One row per Gmail message the poll has looked at — including the ones it
-- ignored. Ignoring must be recorded, or an email that was skipped by mistake
-- is indistinguishable from one that never arrived (evaluation_plan.md §9).
-- ---------------------------------------------------------------------------

create table if not exists email_events (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null references users(id) on delete cascade,

  gmail_message_id  text not null,
  lane              text,           -- payment | spend | application | null
  outcome           text not null,  -- payment | unparsed_payment
                                    -- spend | spend_discarded | unparsed_spend
                                    -- application_applied | application_rejected
                                    -- unparsed_application | ignored | error

  sender            text,
  subject           text,

  -- Extracted scalars ONLY: {amount, spent_at, card, company, role, ...}.
  -- Never the body, never the snippet, never the raw HTML (§4).
  detail            jsonb not null default '{}'::jsonb,

  created_at        timestamptz not null default now()
);

-- The idempotency guarantee, and the reason a retried or re-listed message
-- never double-writes. The poll claims a message by inserting here; a 23505
-- means somebody already has it. Same mechanism as messages_dedupe_idx.
create unique index if not exists email_events_dedupe_idx
  on email_events (user_id, gmail_message_id);

create index if not exists email_events_recent_idx
  on email_events (user_id, created_at desc);

-- ---------------------------------------------------------------------------
-- applications
--
-- Job applications, tracked from their emails. Two rules decide the shape of
-- this table, and both are consequences of what the emails actually look like:
--
--   1. company_key, not (company, role), is what matching runs on. A rejection
--      email routinely names a different role string from the receipt, or
--      none at all — so keying on role guarantees the rejection fails to find
--      the application it exists to close.
--
--   2. The company index is NOT unique. Applying to one company twice must
--      stay expressible; which row an email attaches to is a decision made in
--      code (capabilities/gmail.py:_upsert_application), where it can be read.
-- ---------------------------------------------------------------------------

create table if not exists applications (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references users(id) on delete cascade,

  company       text not null,        -- as written in the email
  company_key   text not null,        -- normalised; what matching runs on

  -- Null is normal, not a failure. A rejection with no matching receipt
  -- creates a row with the company and no role rather than dropping the
  -- signal — an orphan you can see beats a rejection you never hear about.
  role          text,

  -- v1 is two values. interview / oa / offer arrive by widening this CHECK
  -- and adding one pattern family to capabilities/gmail_parse.py.
  status        text not null default 'applied'
                  check (status in ('applied','rejected')),

  source        text not null default 'gmail',
  source_ref    text,                 -- the gmail id that LAST moved this row

  last_email_at timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- Deliberately not unique — see note 2 above. source_ref has no unique index
-- either: it is overwritten every time an email moves the row, so uniqueness
-- on it would reject the updates this table exists to record. email_events is
-- where "the same email never acts twice" is enforced.
create index if not exists applications_company_idx
  on applications (user_id, company_key, created_at desc);

drop trigger if exists applications_touch on applications;
create trigger applications_touch before update on applications
  for each row execute function touch_updated_at();

-- ---------------------------------------------------------------------------
-- Row Level Security — the bot uses service_role and bypasses this. These
-- exist for the Phase 2 browser client, same as 001_init.sql.
-- ---------------------------------------------------------------------------

alter table email_events enable row level security;
alter table applications enable row level security;

drop policy if exists own_email_events on email_events;
create policy own_email_events on email_events
  for all using (user_id in (select id from users where auth_id = auth.uid()));

drop policy if exists own_applications on applications;
create policy own_applications on applications
  for all using (user_id in (select id from users where auth_id = auth.uid()));
