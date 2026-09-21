-- ============================================================================
-- 003_expenses.sql  ·  Phase 1c schema — evaluation_plan.md §10.3
--
-- Shipped ahead of the expense capability itself, because Phase 1b's Gmail
-- poll writes card BILL PAYMENTS (kind='cc_payment') and has nowhere else to
-- put them. Only that subset is exercised today; the rest is here so that
-- Phase 1c is a capability file rather than a second migration against a table
-- that already holds live rows.
--
-- A card bill payment is NOT a category (PLAN-EXPENSES.md §0). It is the
-- settlement of swipes already counted, so it is stored and excluded from
-- every spending total by the `kind` filter. A category for it would count
-- your month twice.
--
-- Run in the Supabase SQL editor after 002_audit_log.sql. Safe to re-run.
-- ============================================================================

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
  -- A cc_payment is exempt: kind <> 'spend', which is why a payment written by
  -- the Gmail poll lands with category null and no constraint to argue with.
  constraint confirmed_spend_needs_category
    check (status <> 'confirmed' or kind <> 'spend' or category is not null)
);

create index if not exists expenses_month_idx on expenses (user_id, spent_at desc);
create index if not exists expenses_owed_idx  on expenses (user_id) where owed_amount > 0;

-- "The same email never produces two rows" as a database guarantee rather than
-- code that has to remember. Partial because chat rows have no source_ref.
create unique index if not exists expenses_dedupe_idx
  on expenses (user_id, source_ref) where source_ref is not null;

-- cards: one jsonb column, not a table (PLAN-EXPENSES.md E8). Card -> config.
-- No totals live here: a stored aggregate drifts across five write paths and
-- a drifted total looks exactly as authoritative as a correct one.
-- Adding a card is an edit to this object, which is why expenses.card has no
-- CHECK constraint: it is validated against these keys at write time instead.
--
-- `match` is the list of strings a bank's emails are recognised by, and it is
-- what capabilities/gmail.py resolves a payment confirmation against. An empty
-- object here means every payment email lands with card = null.
alter table users add column if not exists cards jsonb not null default '{}'::jsonb;

-- No budgets column. Out of scope until the metrics are decided.

drop trigger if exists expenses_touch on expenses;
create trigger expenses_touch before update on expenses
  for each row execute function touch_updated_at();

alter table expenses enable row level security;

drop policy if exists own_expenses on expenses;
create policy own_expenses on expenses
  for all using (user_id in (select id from users where auth_id = auth.uid()));

-- ---------------------------------------------------------------------------
-- Seed the card config (PLAN-EXPENSES.md §0). Edit the match strings to the
-- exact wording your banks use, then re-run this statement alone.
-- `closes` is the statement close day; it stays null until you fill it in and
-- only E9's reconciliation reads it.
-- ---------------------------------------------------------------------------

update users set cards = '{
  "amex_everyday": {"type": "credit", "match": ["American Express", "AMEX"], "closes": null},
  "discover":      {"type": "credit", "match": ["Discover"],                 "closes": null},
  "chase_freedom": {"type": "credit", "match": ["Chase Freedom"],            "closes": null},
  "apple_card":    {"type": "credit", "match": ["Apple Card", "Goldman"],    "closes": null},
  "chase_debit":   {"type": "debit",  "match": ["Chase"],                    "closes": null},
  "sofi_debit":    {"type": "debit",  "match": ["SoFi"],                     "closes": null},
  "cash":          {"type": "cash",   "match": [],                           "closes": null},
  "splitwise":     {"type": "owed",   "match": [],                           "closes": null}
}'::jsonb
where cards = '{}'::jsonb;

-- ---------------------------------------------------------------------------
-- Adding a card to an existing row. This is what E8 means by "a config edit
-- rather than a migration": the seed above only fires on an empty object, so
-- a card added later is merged in. Safe to re-run — the WHERE makes it a no-op
-- once the key exists.
--
-- `splitwise` is money YOU owe someone else. Note the direction: it is the
-- opposite of expenses.owed_amount, which is what someone owes YOU. Its type
-- is deliberately not 'credit', because only a credit card has a bill —
-- log_expense refuses a cc_payment against anything else, and paying a person
-- back is settle_split, never a bill payment.
-- ---------------------------------------------------------------------------

update users
   set cards = cards || '{"splitwise": {"type": "owed", "match": [], "closes": null}}'::jsonb
 where not cards ? 'splitwise';
