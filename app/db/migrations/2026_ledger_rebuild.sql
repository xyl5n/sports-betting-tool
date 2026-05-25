-- ============================================================
-- Migration: rebuild the Model + My Bets systems as proper, fully
-- independent Supabase ledgers with bankroll separated from frozen
-- per-bet stakes.
--
-- WHY:
--  * The model bankroll kept reverting to $739 on every Railway
--    redeploy because it was read from the git-tracked file
--    data/ledger.json.  Bankroll now lives ONLY in Supabase.
--  * A placed bet's stake must be FROZEN at placement.  Previously the
--    recommended amount was recalculated live from the current
--    bankroll, so editing the bankroll changed the amount shown on
--    bets that had already finished.
--  * The model bankroll is now a SINGLE combined $1000 pool across all
--    sports (no per-sport split); per-sport performance breakdowns stay
--    in the separate model_picks table.
--
-- FOUR tables, two per system, never mixed:
--   model_bankroll_pool      one combined pool row (id='model')
--   model_ledger_bets        model staked bets (active + settled)
--   personal_bankroll_pool   one personal pool row (id='personal')
--   personal_ledger_bets     personal tracked bets (active + settled)
--
-- ┌──────────────────────────────────────────────────────────┐
-- │ HOW TO RUN (once, manually):                               │
-- │  1. Log in to Supabase (https://supabase.com).             │
-- │  2. Open your project -> SQL Editor -> "New query".        │
-- │  3. Paste this whole file -> click "Run".                  │
-- │ Safe to re-run (every CREATE is IF NOT EXISTS).            │
-- │                                                            │
-- │ The app seeds the starting bankrolls automatically on its  │
-- │ first boot after these tables exist (My Bets = $166.55,    │
-- │ Model = one combined $1000); it never overwrites a pool    │
-- │ that already has a row, so balances survive redeploys.     │
-- └──────────────────────────────────────────────────────────┘
-- ============================================================

-- ── Model: one combined bankroll pool (NOT split by sport) ──────────
CREATE TABLE IF NOT EXISTS public.model_bankroll_pool (
  id               text PRIMARY KEY DEFAULT 'model',   -- singleton row
  current_balance  double precision NOT NULL,
  starting_balance double precision NOT NULL,
  updated_at       timestamptz DEFAULT now()
);

-- ── Model: staked bets.  stake is FROZEN at placement and never
--    recalculated.  status active -> settled in the 15-minute cycle. ──
CREATE TABLE IF NOT EXISTS public.model_ledger_bets (
  bet_id      text PRIMARY KEY,                 -- deterministic placement key
  placed_date date NOT NULL,                    -- ET date the bet was placed
  sport       text NOT NULL,                    -- 'mlb' | 'wnba' | ...
  kind        text NOT NULL DEFAULT 'game',     -- 'game' | 'prop'
  bet_type    text,                             -- ml | rl | total | <prop market>
  selection   text,                             -- pick/side shown at placement
  odds        integer,                          -- American odds, frozen at placement
  stake       double precision NOT NULL,        -- FROZEN at placement
  status      text NOT NULL DEFAULT 'active',   -- 'active' | 'settled'
  result      text,                             -- win | loss | push | void | NULL
  profit      double precision,                 -- +profit(win) / -stake(loss) / 0(push|void)
  game_id     text,
  player_name text,                             -- props only
  meta        jsonb,
  placed_at   timestamptz DEFAULT now(),
  settled_at  timestamptz
);
CREATE INDEX IF NOT EXISTS model_ledger_bets_status_idx ON public.model_ledger_bets (status);
CREATE INDEX IF NOT EXISTS model_ledger_bets_date_idx   ON public.model_ledger_bets (placed_date DESC);
CREATE INDEX IF NOT EXISTS model_ledger_bets_sport_idx  ON public.model_ledger_bets (sport);

-- ── My Bets: one personal bankroll pool + the 4 AM daily-limit snapshot.
--    daily_limit is refreshed once per day at 4 AM ET off the current
--    bankroll that morning; it sizes NEW bets only and never alters an
--    already-placed bet's frozen stake. ──────────────────────────────
CREATE TABLE IF NOT EXISTS public.personal_bankroll_pool (
  id               text PRIMARY KEY DEFAULT 'personal',   -- singleton row
  current_balance  double precision NOT NULL,
  starting_balance double precision NOT NULL,
  daily_limit      double precision,            -- 4 AM ET snapshot off current bankroll
  daily_limit_date date,                        -- ET date the snapshot was taken
  updated_at       timestamptz DEFAULT now()
);

-- ── My Bets: personal tracked bets.  Same frozen-stake contract. ────
CREATE TABLE IF NOT EXISTS public.personal_ledger_bets (
  bet_id      text PRIMARY KEY,
  placed_date date NOT NULL,
  sport       text NOT NULL,
  kind        text NOT NULL DEFAULT 'game',
  bet_type    text,
  selection   text,
  odds        integer,
  stake       double precision NOT NULL,        -- FROZEN at placement
  status      text NOT NULL DEFAULT 'active',   -- 'active' | 'settled'
  result      text,                             -- win | loss | push | void | NULL
  profit      double precision,                 -- +profit(win) / -stake(loss) / 0(push|void)
  game_id     text,
  player_name text,
  meta        jsonb,
  placed_at   timestamptz DEFAULT now(),
  settled_at  timestamptz
);
CREATE INDEX IF NOT EXISTS personal_ledger_bets_status_idx ON public.personal_ledger_bets (status);
CREATE INDEX IF NOT EXISTS personal_ledger_bets_date_idx   ON public.personal_ledger_bets (placed_date DESC);
