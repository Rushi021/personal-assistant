 -- ============================================================================
-- 001_init.sql  ·  Personal Assistant V1
--
-- DRAFT. Expect to rename columns and add more in week one — that is the plan,
-- not a failure of it. `meta jsonb` on tasks exists so you can try a new field
-- without a migration at all; promote it to a real column once you keep using it.
--
-- Run in the Supabase SQL editor. Safe to re-run.
-- ============================================================================

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- users
--
-- One row for now. The table exists so that "a second person" is a row,
-- not a migration. `auth_id` stays null until the web app + Supabase Auth land.
-- ---------------------------------------------------------------------------

create table if not exists users (
  id                 uuid primary key default gen_random_uuid(),
  auth_id            uuid unique,                        -- set when Supabase Auth arrives
  channel            text not null default 'telegram',   -- telegram | whatsapp
  channel_user_id    text not null,                      -- telegram chat id, or E.164 phone
  display_name       text,

  -- SET THIS BEFORE ANYTHING ELSE. It decides what "today" means, and every
  -- rollover, deadline and check-in is computed from it. IANA name, e.g.
  -- 'Asia/Kolkata', 'Europe/London'. Getting it wrong silently breaks the
  -- core feature at midnight and nowhere else.
  timezone           text not null default 'UTC',

  weekly_budget_min  int  not null default 720,          -- 12 hours of real task time
  slip_threshold     int  not null default 3,            -- slips before it challenges you
  last_nudge_on      date,                               -- nag once a day, not once a message
  checkin_hour       int  not null default 11,           -- fallback check-in, user's local time

  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),

  unique (channel, channel_user_id)
);

-- ---------------------------------------------------------------------------
-- tasks
--
-- Four independent axes, deliberately not collapsed into each other:
--   priority_level  -> ORDER      (what comes first when the day is short)
--   category        -> GROUPING   (the morning brief; Gmail tagging its finds)
--   estimate_min    -> CAPACITY   (is the week actually full)
--   deadline_hard   -> NEGOTIABLE (can this be moved at all)
-- A card payment is money / high / quick / hard all at once.
--
-- planned_on vs due_on are NOT the same thing:
--   due_on     = the world's deadline  (the bill is due Friday)
--   planned_on = your intention        (you'll pay it Wednesday)
-- A row with a due_on and no planned_on is a deadline you haven't scheduled —
-- precisely the thing that gets forgotten, so it's queryable on its own.
-- ---------------------------------------------------------------------------

create table if not exists tasks (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null references users(id) on delete cascade,

  title          text not null,
  notes          text,

  status         text not null default 'todo'
                   check (status in ('todo','in progress','done','dropped')),

  planned_on     date,                                   -- null = captured, not scheduled
  due_on         date,                                   -- null = no external deadline
  deadline_hard  boolean not null default false,         -- true = real consequence if missed

  estimate_min   int  not null default 25,               -- quick 5 · short 25 · deep 90
  priority_level text not null default 'normal'
                   check (priority_level in ('high','normal','low')),

  -- free text on purpose: money | admin | work | study | health | life, and a
  -- seventh next month without a migration. Add a CHECK later if it drifts.
  category       text,

  slip_count     int  not null default 0,                -- times rolled forward
  source         text not null default 'chat'            -- chat | voice | gmail | import
                   check (source in ('chat','voice','gmail','sheets','import')),
  source_ref     text,                                   -- gmail message id, sheet row, etc.

  meta           jsonb not null default '{}'::jsonb,     -- the escape hatch

  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  completed_at   timestamptz
);

-- The rollover query runs every morning and must stay one indexed lookup:
-- "everything still open that was planned on or before <date>".
create index if not exists tasks_open_planned_idx
  on tasks (user_id, planned_on)
  where status in ('todo','in progress');

-- Deadline sweep: what's coming due, scheduled or not.
create index if not exists tasks_open_due_idx
  on tasks (user_id, due_on)
  where status in ('todo','in progress') and due_on is not null;

-- The inbox: captured and never given a day. This is the pile that rots.
create index if not exists tasks_unplanned_idx
  on tasks (user_id, created_at)
  where status = 'todo' and planned_on is null;

-- ---------------------------------------------------------------------------
-- messages
--
-- Conversation memory, audit trail, and the cost ledger. `meta` carries
-- model, tokens and cost_usd per turn so the ~$13/month estimate is a
-- measured number instead of a promise.
-- ---------------------------------------------------------------------------

create table if not exists messages (
  id              uuid primary key default gen_random_uuid(),
  user_id         uuid not null references users(id) on delete cascade,

  role            text not null check (role in ('user','assistant','system')),
  content         text not null,

  -- Telegram and WhatsApp both retry webhooks. The unique index below turns
  -- "don't process the same message twice" into a database guarantee rather
  -- than application code that has to remember to be careful.
  channel_msg_id  text,

  meta            jsonb not null default '{}'::jsonb,
  created_at      timestamptz not null default now()
);

create index if not exists messages_recent_idx
  on messages (user_id, created_at desc);

create unique index if not exists messages_dedupe_idx
  on messages (user_id, channel_msg_id)
  where channel_msg_id is not null;

-- ---------------------------------------------------------------------------
-- updated_at
-- ---------------------------------------------------------------------------

create or replace function touch_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists users_touch on users;
create trigger users_touch before update on users
  for each row execute function touch_updated_at();

drop trigger if exists tasks_touch on tasks;
create trigger tasks_touch before update on tasks
  for each row execute function touch_updated_at();

-- ---------------------------------------------------------------------------
-- Row Level Security
--
-- The bot server connects with the service_role key and BYPASSES all of this.
-- These policies exist for the browser client in Phase 2, which will talk to
-- Supabase directly. Writing them now costs ten lines; retrofitting them onto
-- a live table with real data does not.
-- ---------------------------------------------------------------------------

alter table users    enable row level security;
alter table tasks    enable row level security;
alter table messages enable row level security;

drop policy if exists own_user on users;
create policy own_user on users
  for all using (auth_id = auth.uid());

drop policy if exists own_tasks on tasks;
create policy own_tasks on tasks
  for all using (user_id in (select id from users where auth_id = auth.uid()));

drop policy if exists own_messages on messages;
create policy own_messages on messages
  for all using (user_id in (select id from users where auth_id = auth.uid()));

-- ---------------------------------------------------------------------------
-- Seed yourself. Edit both placeholders before running.
-- Get your Telegram chat id by messaging @userinfobot.
-- ---------------------------------------------------------------------------

insert into users (channel, channel_user_id, display_name, timezone)
values ('telegram', 'REPLACE_WITH_YOUR_CHAT_ID', 'Rushi', 'UTC')
on conflict (channel, channel_user_id) do nothing;
