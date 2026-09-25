# Graph Report - personal-assistant  (2026-09-24)

## Corpus Check
- 40 files · ~54,902 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 749 nodes · 1520 edges · 61 communities (37 shown, 23 thin omitted)
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 40 edges (avg confidence: 0.85)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Fast Path Day Loop
- Live Acceptance Scenarios
- Tasks and Planning
- Expense Query Helpers
- Gmail Parse Extractors
- Channel Choice Decisions
- Gmail Rule Tests
- Phase 1 Acceptance Tests
- Supabase Query Client
- Gmail OAuth Client
- Model Cost and Dispatch
- Flow and Trust Tests
- FastAPI Ingress Routes
- Job Application Parsing
- V1 Architecture Decisions
- Fast Path Protocol Choices
- Evaluation and Non-goals
- iCloud Calendar Sync
- Email Lane Routing
- Turn Audit Logging
- Core Table Schema
- Hosting and Model Stack
- Gmail Dedupe and Upsert
- Free Tier Model Rotation
- Turn Lifecycle Ingress
- Capacity and Budget Rules
- Test Database Stand-in
- Capability Plugin System
- Payment Amount Parsing
- Recurrence Date Clamping
- Expense Schema Columns
- Application Lifecycle Tests
- Coding Guidelines Skill
- Card Bill Payments
- CalDAV Calendar Discovery
- Tool Registry Tracing
- Payment Routing Guards
- Planned Calendar Gate
- Calendar Failure Isolation
- Missing Speech Key Error
- Unbacked Claim Guard
- Universal Tool Tracing
- Fake Database Stub
- Reminder Roll Forward
- Slip Count Reset
- Idempotent Task Completion
- Invalid Recurrence Refusal
- Expense Split Example
- Payback Amount Isolation
- Bill Excluded From Spend
- Credit Card Bill Guard
- Unknown Card Refusal
- Chat Pending Attachment
- Ambiguous Amount Matching
- Chat-First Email Discard
- Empty Day Digest Silence
- Deterministic Digest Math
- Daily Tick Integration
- Mailbox Identity Isolation
- Open Architecture Items

## God Nodes (most connected - your core abstractions)
1. `sb()` - 48 edges
2. `_run()` - 35 edges
3. `handle_turn()` - 29 edges
4. `scenario()` - 26 edges
5. `local_today()` - 24 edges
6. `step()` - 23 edges
7. `Architecture V1` - 23 edges
8. `Gmail Ingest Plan` - 23 edges
9. `webhook()` - 21 edges
10. `day_status()` - 20 edges

## Surprising Connections (you probably didn't know these)
- `owed_amount` --conceptually_related_to--> `settle_split()`  [EXTRACTED]
  evaluation_plan.md → capabilities/expenses.py
- `share_amount` --rationale_for--> `settle_split()`  [EXTRACTED]
  evaluation_plan.md → capabilities/expenses.py
- `WhatsApp Cloud API` --conceptually_related_to--> `Channel`  [INFERRED]
  ARCHITECTURE-V1.md → core/channels/base.py
- `local_today()` --shares_data_with--> `users Table`  [INFERRED]
  core/db.py → ARCHITECTURE-V1.md
- `spent_at From the Alert` --conceptually_related_to--> `local_today()`  [EXTRACTED]
  PLAN-EXPENSES.md → core/db.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Locked V1 Foundation Decisions** — architecture_v1_d1_voice_message_type, architecture_v1_d2_sdk_tool_runner, architecture_v1_d3_one_file_capability, architecture_v1_d4_fast_path, architecture_v1_d5_three_table_schema, architecture_v1_d6_telegram_not_whatsapp, architecture_v1_d7_model_routing, architecture_v1_d8_capture_never_blocked, architecture_v1_d9_capacity_in_minutes, architecture_v1_d10_a2a_rejected, architecture_v1_d11_swappable_channel [EXTRACTED 1.00]
- **Gmail Poll Three Lanes** — capabilities_gmail_poll, capabilities_gmail_parse_route, plan_gmail_lane_a, plan_gmail_lane_b, plan_gmail_lane_c, plan_gmail_email_events [EXTRACTED 1.00]
- **Spend Amount Share Owed and Kind** — evaluation_plan_amount, evaluation_plan_share_amount, evaluation_plan_owed_amount, plan_expenses_kind [EXTRACTED 1.00]

## Communities (61 total, 23 thin omitted)

### Community 0 - "Fast Path Day Loop"
Cohesion: 0.05
Nodes (79): _fast_path(), D4 — a button tap is a deterministic parse and a direct write. No model, no…, digest(), Today's money, or None when there is nothing to say. Returning None matters: an…, _poll_account(), A swipe. The amount, card and date count immediately; the meaning is blank…, _write_spend(), day_status() (+71 more)

### Community 1 - "Live Acceptance Scenarios"
Cohesion: 0.08
Nodes (56): a1(), a2(), a3(), a4(), b1(), b2(), b3(), c1() (+48 more)

### Community 2 - "Tasks and Planning"
Cohesion: 0.08
Nodes (41): _bulk_card(), Planning — rollover, capacity, the nag (D8, D9). Mostly a PROMPT plus one read-…, add_task(), complete_task(), drop_task(), list_tasks(), beta_tool, Tasks — Phase 1a. Six tools, no more. A capability is one file (D3): TOOLS is… (+33 more)

### Community 3 - "Expense Query Helpers"
Cohesion: 0.14
Nodes (30): _attach(), _by_category(), card_summary(), _cards(), _check_card(), list_applications(), list_expenses(), log_expense() (+22 more)

### Community 4 - "Gmail Parse Extractors"
Cohesion: 0.12
Nodes (26): _any(), _ats_local_part(), _clean(), _find_company(), _find_date(), _find_role(), is_application(), is_payment() (+18 more)

### Community 5 - "Channel Choice Decisions"
Cohesion: 0.08
Nodes (21): D11 Channel Is Swappable, D6 Telegram Not WhatsApp, Phase 1a, S7 11am Check-in, WhatsApp API Pricing 2026, WhatsApp Service Message Pricing Oct 2026, Telegram Bot API, WhatsApp Cloud API (+13 more)

### Community 6 - "Gmail Rule Tests"
Cohesion: 0.10
Nodes (23): Phase 1b checks — the email rules, offline. python test_gmail.py # or: pytest…, The one that carries praise. The praise is not what classifies it., The praise-only guard is an absence, not a function. Anything matching a…, evaluation_plan.md §4 is a trust boundary. The body is decoded, handed to the…, The whole economic argument for rules over extraction. A model import in this…, The trap: the check-in loop `continue`s on checkin_hour, so a poll inside that…, The guard the whole REJECTION_PATTERNS list is shaped around: praise appears in…, The common real shape. A subject-only scan calls this one 'applied'. (+15 more)

### Community 7 - "Phase 1 Acceptance Tests"
Cohesion: 0.13
Nodes (20): Phase 1 checks — recurring reminders, expenses, and the digest. Offline. python…, Run fn(store) with core.db.sb() and the per-turn context swapped out., _run(), test_a_card_payment_never_gets_a_category(), test_a_spend_email_with_no_chat_row_lands_as_pending(), test_an_ordinary_task_still_just_closes(), test_an_unknown_category_is_refused(), test_by_category_breakdown_excludes_settlements() (+12 more)

### Community 8 - "Supabase Query Client"
Cohesion: 0.09
Nodes (5): _is_duplicate(), Exception, _Query, Enough of the supabase-py chain for the calls these modules actually make.…, _Result

### Community 9 - "Gmail OAuth Client"
Cohesion: 0.15
Nodes (16): _access_token(), accounts(), _api(), configured(), _consent(), _list_ids(), _message(), Gmail — the hourly poll, the router, and the two lanes' write paths (Phase 1b).… (+8 more)

### Community 10 - "Model Cost and Dispatch"
Cohesion: 0.12
Nodes (18): cost(), model, prompt_hash, and the running token/cost tally from turn.py., What this turn cost, in dollars. Makes the ~$13/month estimate a measured…, usage(), calls_in(), contents_from(), dispatch(), Stored history → a valid Gemini contents array: the assistant is "model",… (+10 more)

### Community 11 - "Flow and Trust Tests"
Cohesion: 0.11
Nodes (18): _grep(), The checks the plan leaves behind, written in the step that created the…, A task planned Sunday and one planned Monday fall in different weeks., The docstring is the tool's whole interface. The summary becomes the tool…, <= 64 bytes is WhatsApp's ceiling; a raw uuid is 36, so a short prefix is all…, §4 is a trust boundary, not a lazy-eligible simplification. Traces are a second…, app.py opens the trace, then runs the turn in asyncio.to_thread, which COPIES…, The trail is only full visibility if each step says what it IS and what it… (+10 more)

### Community 12 - "FastAPI Ingress Routes"
Cohesion: 0.16
Nodes (14): _deliver(), health(), FastAPI ingress. Routes, never logic. Two rules this file exists to keep: - it…, Returns 200 no matter what. A non-200 makes Telegram redeliver, and a…, tick(), webhook(), send(), set_source() (+6 more)

### Community 13 - "Job Application Parsing"
Cohesion: 0.12
Nodes (17): company_key(), parse_application(), The matching key. Every spelling of one employer must collapse to one. Spaces…, Classify an application email and pull out who it is from. Three cases, and the…, The ATS domain identifies greenhouse, not the employer — so the display name is…, for <title>' is a role; 'at <company>' is a company. The templates are…, The duplicate-row bug: an ATS slug and a human-written name are the same…, test_a_job_title_is_never_taken_as_the_employer() (+9 more)

### Community 14 - "V1 Architecture Decisions"
Cohesion: 0.17
Nodes (16): Architecture V1, D1 Voice Is a Message Type, D8 Capture Is Never Blocked, D9 Capacity Is Minutes, Groq Whisper, Reconciliation Flow, S1 Conversational Tasks, S2 Reconcile Yesterday First (+8 more)

### Community 15 - "Fast Path Protocol Choices"
Cohesion: 0.16
Nodes (15): Agent2Agent Protocol, D10 A2A Is Not Tooling, D4 Fast Path Is the Default, Fast Path, Model Context Protocol, Phase 1b, S4 Done Without an LLM, S5 Gmail Deadlines Become Tasks (+7 more)

### Community 16 - "Evaluation and Non-goals"
Cohesion: 0.22
Nodes (15): V1 Non-goals, Row Level Security, Evaluation Plan, Log Trust Boundary, Expense Tracking Plan, E5 No SMS, E7 kind Is Highest Risk, E9 Bill Is a Bound (+7 more)

### Community 17 - "iCloud Calendar Sync"
Cohesion: 0.18
Nodes (14): configured(), _esc(), _fold(), _ics(), Calendar — the day's work, made visible (Apple iCloud over CalDAV). One rule…, Make the calendar agree with this task row. Idempotent, and derives the event…, Does this task belong on the calendar? The whole rule, in one place so that…, RFC 5545 §3.3.11. An unescaped comma silently truncates the summary. (+6 more)

### Community 18 - "Email Lane Routing"
Cohesion: 0.14
Nodes (15): is_spend(), Which lane, if any. Order matters and is the point of the function. Payment is…, route(), E1 Extract on Arrival, Lane C Swipe Alerts, Order matters: a card issuer's 'thank you for your payment' must never reach…, no-reply is a hint, not a gate. Rejections arrive from careers@ and from named…, Thanks for your interest in Apple.' is an ad. The phrase is genuine in both… (+7 more)

### Community 19 - "Turn Audit Logging"
Cohesion: 0.18
Nodes (14): _dim(), drop(), _emit(), prompt_hash(), One audit_log row per turn: what came in, every step taken, what went out.…, Everything the step carried except its type and timestamp — which for a tool is…, Never raises. Losing the trace must not cost the reply — same rule as the…, An update rejected before a trace could open: bad secret, unparseable update,… (+6 more)

### Community 20 - "Core Table Schema"
Cohesion: 0.22
Nodes (14): channel_msg_id, D5 Three Tables and JSONB, due_on, last_nudge_on, messages Table, planned_on, source_ref, tasks Table (+6 more)

### Community 21 - "Hosting and Model Stack"
Cohesion: 0.18
Nodes (14): D7 Route Models by Path, Fly.io Hosting, mistral-small-latest, Supabase Free Tier Limits 2026, Supabase Postgres, G11 Raw REST over httpx, A2 Python FastAPI Supabase Stack, A3 Deploy Fly on Day One (+6 more)

### Community 22 - "Gmail Dedupe and Upsert"
Cohesion: 0.21
Nodes (14): Match an application email to a row, or start one. Two rules, both judgement…, _upsert_application(), spent_at From the Alert, Gmail Ingest Plan, applications Table, G10 spent_at From the Email, G2 Two Dedupe Layers, G4 Any Rejection Phrase Wins (+6 more)

### Community 23 - "Free Tier Model Rotation"
Cohesion: 0.19
Nodes (13): Exhausted, generate(), _order(), _pace(), _park(), Free-tier model rotation. The Gemini free tier caps requests per DAY per model,…, What the ladder looks like right now — for the terminal, and for a test run…, Every model is out of free-tier quota. Distinct from a crash because the honest… (+5 more)

### Community 24 - "Turn Lifecycle Ingress"
Cohesion: 0.24
Nodes (13): _ingress(), The first two steps of every turn: what Telegram actually sent, and who it…, poll(), One pass over the window, across every configured mailbox. Returns how many…, begin(), finish(), Append one event to the trail. A no-op when no turn is in flight, so tests and…, redact() (+5 more)

### Community 25 - "Capacity and Budget Rules"
Cohesion: 0.15
Nodes (13): A6 — day_status hands back a boolean. Minutes decide when to speak; they are…, If a second path ever appears here, the seam is gone., Telegram answers 400 with a reason and the adapter used to discard it: the turn…, The free tier runs out every day. "Try again" is a lie then — nothing the user…, D11's two unretrofittable rules, checked where they would be broken., Under 60 lines of actual code. Counted without comments or docstrings so that…, _read(), test_budget_never_speaks() (+5 more)

### Community 26 - "Test Database Stand-in"
Cohesion: 0.15
Nodes (3): _Fake, _Query, _Result

### Community 27 - "Capability Plugin System"
Cohesion: 0.17
Nodes (12): Capability Registry, D2 SDK Tool Runner Not MCP, D3 Capability Is One File, meta JSONB, pending_question, Anthropic Advanced Tool Use, MCP Gateways Latency, MCP vs Function Calling (+4 more)

### Community 28 - "Payment Amount Parsing"
Cohesion: 0.20
Nodes (10): parse_payment(), Pull amount, date and card out of a payment confirmation. Returns None when no…, No date in the email means None, so the caller uses Gmail's own receipt time.…, Reference ids and account digits are bare numbers. Without the cents…, Chase' and 'Chase Freedom' are both in the config. The short one must not claim…, test_amount_requires_cents_so_digits_are_not_amounts(), test_longest_card_match_wins(), test_payment_amount_and_date() (+2 more)

### Community 29 - "Recurrence Date Clamping"
Cohesion: 0.22
Nodes (10): _month_day(), next_occurrence(), date, Clamp to the month's real length. 'monthly:31' in February is the 28th (or…, The next date strictly after `after`, or None if the rule is unusable. An…, A bill reminder that silently skips a month is the expensive kind of wrong. The…, test_an_unusable_rule_makes_a_normal_task_not_an_error(), test_monthly_31_clamps_instead_of_skipping_february() (+2 more)

### Community 30 - "Expense Schema Columns"
Cohesion: 0.20
Nodes (10): amount, expenses Table, owed_amount, share_amount, 001_init.sql, 002_audit_log.sql, 003_expenses.sql, 005_recurring.sql (+2 more)

### Community 31 - "Application Lifecycle Tests"
Cohesion: 0.20
Nodes (10): Run fn(store) with core.db.sb() swapped for the stand-in., The acceptance criterion: exactly one cc_payment row per gmail id, even if the…, One company, two emails, one row — and it ends up rejected. Matching ignores…, No receipt ever arrived. The company comes off the subject or sender, the role…, test_a_rejection_closes_the_application_its_receipt_opened(), test_a_repeated_receipt_does_not_open_a_second_application(), test_an_orphan_rejection_still_becomes_a_visible_row(), test_an_unreadable_payment_writes_nothing_and_says_so() (+2 more)

### Community 32 - "Coding Guidelines Skill"
Cohesion: 0.25
Nodes (8): Karpathy Guidelines, Andrej Karpathy, Goal-Driven Execution, Surgical Changes, Think Before Coding, evals/cases.jsonl, Failure to Fix Loop, verdict

### Community 33 - "Card Bill Payments"
Cohesion: 0.40
Nodes (6): A settlement, not a spend. kind='cc_payment' is excluded from every spending…, _write_payment(), Card Configuration, E8 Cards Are Config, G9 Match Cards Only via users.cards, Lane A Card Bill Payments

### Community 34 - "CalDAV Calendar Discovery"
Cohesion: 0.40
Nodes (5): discover(), _propfind(), Client, Walk current-user-principal -> calendar-home-set -> the calendar list., Element

### Community 35 - "Tool Registry Tracing"
Cohesion: 0.50
Nodes (3): Capability auto-discovery — the entire plugin system (D3). Adding Gmail means…, Log every tool invocation from one place. Every call the SDK makes goes through…, _traced()

### Community 36 - "Payment Routing Guards"
Cohesion: 0.33
Nodes (4): E7 at the router. Both families mention cards and amounts; the specific one has…, Two code paths for finishing a task means a recurring reminder rolls forward…, test_a_payment_confirmation_is_never_routed_as_a_spend(), test_the_done_button_and_the_tool_share_one_completion_path()

## Ambiguous Edges - Review These
- `Gmail Ingest Plan` → `S5 Gmail Deadlines Become Tasks`  [AMBIGUOUS]
  PLAN-GMAIL.md · relation: conceptually_related_to
- `Python Requirements` → `A2 Python FastAPI Supabase Stack`  [AMBIGUOUS]
  PLAN-V1.md · relation: conceptually_related_to
- `D2 SDK Tool Runner Not MCP` → `D7 Route Models by Path`  [AMBIGUOUS]
  ARCHITECTURE-V1.md · relation: conceptually_related_to
- `D7 Route Models by Path` → `A2 Python FastAPI Supabase Stack`  [AMBIGUOUS]
  PLAN-V1.md · relation: conceptually_related_to

## Knowledge Gaps
- **27 isolated node(s):** `Andrej Karpathy`, `Phase 1a`, `V1 Non-goals`, `Agent2Agent Protocol`, `Tool Search and defer_loading` (+22 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 286 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **23 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Gmail Ingest Plan` and `S5 Gmail Deadlines Become Tasks`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `Python Requirements` and `A2 Python FastAPI Supabase Stack`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `D2 SDK Tool Runner Not MCP` and `D7 Route Models by Path`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `D7 Route Models by Path` and `A2 Python FastAPI Supabase Stack`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `handle_turn()` connect `Model Cost and Dispatch` to `Fast Path Day Loop`, `Coding Guidelines Skill`, `Tasks and Planning`, `Live Acceptance Scenarios`, `Channel Choice Decisions`, `Flow and Trust Tests`, `FastAPI Ingress Routes`, `V1 Architecture Decisions`, `Hosting and Model Stack`, `Free Tier Model Rotation`, `Turn Lifecycle Ingress`, `Capability Plugin System`?**
  _High betweenness centrality (0.095) - this node is a cross-community bridge._
- **Why does `sb()` connect `Fast Path Day Loop` to `Card Bill Payments`, `Tasks and Planning`, `Expense Query Helpers`, `Live Acceptance Scenarios`, `Turn Audit Logging`, `Gmail Dedupe and Upsert`, `Turn Lifecycle Ingress`?**
  _High betweenness centrality (0.088) - this node is a cross-community bridge._
- **Why does `_Query` connect `Supabase Query Client` to `Phase 1 Acceptance Tests`?**
  _High betweenness centrality (0.045) - this node is a cross-community bridge._