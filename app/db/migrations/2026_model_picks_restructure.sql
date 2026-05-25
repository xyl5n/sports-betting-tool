-- ============================================================
-- Migration: restructure model_picks into the single per-model
-- performance store (one row per model pick, all sports/markets).
--
-- WHY: the old model_picks table is missing the pick_id column,
-- which throws "PGRST204 could not find pick_id" on every cycle so
-- nothing was ever saved.  The JSON trackers don't survive Railway
-- redeploys, which is why records keep resetting to 0-0.  This makes
-- Supabase the single source of truth.  No data is backfilled —
-- every store starts fresh from here and fills in as games settle.
--
-- ┌──────────────────────────────────────────────────────────┐
-- │ HOW TO RUN (do this once, manually):                       │
-- │  1. Log in to Supabase (https://supabase.com).             │
-- │  2. Open your project → SQL Editor → "New query".          │
-- │  3. Paste this ENTIRE file into the editor.                │
-- │  4. Click "Run".                                           │
-- │ It is safe to re-run.  The old model_picks table held no   │
-- │ saved data (the schema error blocked all writes), so this  │
-- │ drops + recreates it cleanly.                              │
-- └──────────────────────────────────────────────────────────┘
-- ============================================================

DROP TABLE IF EXISTS public.model_picks;

CREATE TABLE public.model_picks (
  pick_id      text        PRIMARY KEY,   -- deterministic: sport:model:bet_type:game_id[:player_name]
  sport        text,                      -- 'mlb' | 'wnba' | ...
  model        text,                      -- 'xgb' | 'lr' | 'nn' | 'combined' | 'pitcher' | 'batter'
  bet_type     text,                      -- 'ml' | 'rl' | 'total' (games) | prop market name (props)
  status       text        DEFAULT 'pending',  -- 'pending' | 'finished'
  pick_side    text,                      -- team name, or 'OVER' / 'UNDER'
  line         numeric,                   -- nullable
  confidence   numeric,
  result       text,                      -- 'win' | 'loss' | 'void' | null while pending
  game_id      text,
  player_name  text,                      -- nullable; set for props
  created_at   timestamptz DEFAULT now(),
  settled_at   timestamptz
);

CREATE INDEX model_picks_filter_idx
  ON public.model_picks (sport, model, status);
