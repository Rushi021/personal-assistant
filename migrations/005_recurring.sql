-- ============================================================================
-- 005_recurring.sql  ·  Phase 1 — recurring reminders + the expense digest hour
--
-- Two columns. Recurrence was deferred in ARCHITECTURE-V1.md §7 and is now
-- needed for the thing it was always going to be needed for: "cancel the
-- subscription on the 15th", "pay the card bill every month".
--
-- The shape is deliberately NOT a template table with materialised instances.
-- A recurring task is ONE row that moves forward when you finish it, and
-- finishing it leaves a dated copy behind as the record. That means:
--   - no materialisation cron, so no "did it run twice" failure mode
--   - no template/instance split, so no orphaned instances when you edit it
--   - your open list never fills with twelve future copies of one reminder
--
-- The cost is that a recurring task you IGNORE does not pile up — it sits on
-- its day and slips, exactly like any other task. That is the behaviour the
-- rollover ritual already handles, which is why this shape was chosen.
--
-- Run in the Supabase SQL editor after 004_email.sql. Safe to re-run.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- tasks.recur
--
-- Null for an ordinary task. Otherwise one of:
--
--   daily              every day
--   weekly:mon         every Monday       (mon tue wed thu fri sat sun)
--   monthly:15         the 15th of every month
--   monthly:last       the last day of every month
--   yearly:03-15       15 March, every year
--
-- Free text rather than a CHECK, for the same reason tasks.category is: the
-- parser in capabilities/tasks.py is the one place that validates it, and
-- adding 'quarterly' later should be a code edit, not a migration. An
-- unparseable rule makes the task behave as a normal one-off; it never throws.
-- ---------------------------------------------------------------------------

alter table tasks add column if not exists recur text;

-- Only recurring tasks are ever looked up by this, and there is one row per
-- reminder, so a partial index is enough to keep "show me my reminders" cheap.
create index if not exists tasks_recurring_idx
  on tasks (user_id, planned_on)
  where recur is not null and status in ('todo','in progress');

-- ---------------------------------------------------------------------------
-- users.digest_hour
--
-- The money message, in the user's local time. Separate from checkin_hour
-- because they are different messages about different things: 11:00 opens the
-- day's work, 21:00 closes the day's spending (E4 — one of each, not more).
-- ---------------------------------------------------------------------------

alter table users add column if not exists digest_hour int not null default 21;

-- ---------------------------------------------------------------------------
-- users.last_digest_on
--
-- The same guard last_nudge_on gives the nag: the tick is hourly, and a digest
-- that fires twice is one you stop reading. Flipped in code when the digest is
-- composed, so it self-guards regardless of how often the tick runs.
-- ---------------------------------------------------------------------------

alter table users add column if not exists last_digest_on date;
