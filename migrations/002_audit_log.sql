-- ============================================================================
-- 002_audit_log.sql  ·  one row per turn — see evaluation_plan.md
--
-- Written in a finally at the app boundary, so a turn that THROWS still leaves
-- a record. That is the whole reason this is its own table and not
-- messages.meta: a crashed turn never reaches save_message(role='assistant'),
-- and crashed turns are exactly the ones worth auditing.
--
-- Run in the Supabase SQL editor after 001_init.sql. Safe to re-run.
-- ============================================================================

create table if not exists audit_log (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid references users(id) on delete cascade,

  turn_ref       text,          -- channel_msg_id — joins a row back to messages
  trigger        text not null, -- message | voice | button | cron
  path           text not null, -- model | fast
  model          text,          -- null on the fast path, and that null is the point
  prompt_hash    text,          -- sha256(identity + prompts + tool schemas)[:12]

  input          text,          -- what the turn was handed (redacted)
  reply          text,          -- what went out
  steps          jsonb not null default '[]'::jsonb,   -- the reasoning trail

  status         text not null default 'ok'
                   check (status in ('ok','error','duplicate')),
  error          text,
  latency_ms     int,

  input_tokens       int not null default 0,
  output_tokens      int not null default 0,
  cache_read_tokens  int not null default 0,
  cost_usd           numeric(10,6) not null default 0,

  -- Your judgement. Set by hand, in the Supabase table editor, on the handful
  -- of turns a day that felt wrong. The eval loop reads from this column and
  -- nothing else populates it.
  verdict        text check (verdict in ('good','bad')),
  verdict_note   text,

  created_at     timestamptz not null default now()
);

create index if not exists audit_recent_idx on audit_log (user_id, created_at desc);

-- The triage queue: everything not yet judged, plus everything judged bad.
create index if not exists audit_triage_idx on audit_log (created_at desc)
  where verdict is null or verdict = 'bad';

-- Six of the eight production invariants in evaluation_plan.md §5 are
-- containment queries over steps: steps @> '[{"t":"decision"}]'. Without this
-- they are sequential scans over every turn you have ever taken.
create index if not exists audit_steps_idx on audit_log using gin (steps jsonb_path_ops);

-- Retention sweep runs from /cron/tick at 03:00 UTC and needs this to be a
-- range scan rather than a full table read.
create index if not exists audit_retention_idx on audit_log (created_at)
  where verdict is null;

alter table audit_log enable row level security;

drop policy if exists own_audit on audit_log;
create policy own_audit on audit_log
  for all using (user_id in (select id from users where auth_id = auth.uid()));
