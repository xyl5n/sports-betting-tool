-- ============================================================
-- Migration: personal_bets — durable home for the My Bets ledger.
--
-- WHY: the personal-bet ledger currently lives in git-tracked local
-- JSON (data/ledger.json, data/wnba_ledger.json), which Railway resets
-- to the committed copy on every redeploy / PR merge — so tracked bets
-- disappear.  This table makes Supabase the source of truth (one JSON
-- blob per sport).  It is kept entirely separate from model_picks:
-- personal_bets drives only the personal bankroll + tracked-bet display.
--
-- Existing bets are preserved automatically: on the first boot after this
-- table exists, the app seeds it from whatever data is already on disk /
-- in the legacy snapshot, then reads from this table thereafter.
--
-- ┌──────────────────────────────────────────────────────────┐
-- │ HOW TO RUN (once, manually):                               │
-- │  1. Log in to Supabase (https://supabase.com).             │
-- │  2. Open your project → SQL Editor → "New query".          │
-- │  3. Paste this whole file → click "Run".                   │
-- │ Safe to re-run (CREATE TABLE IF NOT EXISTS).               │
-- └──────────────────────────────────────────────────────────┘
-- ============================================================

CREATE TABLE IF NOT EXISTS public.personal_bets (
  sport      text PRIMARY KEY,          -- 'mlb' | 'wnba' | ...
  data       jsonb,                     -- full ledger JSON (open_bets, history, bankroll)
  updated_at timestamptz DEFAULT now()
);
