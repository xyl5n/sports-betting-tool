-- ============================================================
-- Supabase schema for the sports-betting app.
-- Run this once in the Supabase SQL editor (project → SQL → New
-- query → paste → Run).  Every CREATE is guarded by IF NOT EXISTS
-- so it's safe to re-run.
--
-- Tables:
--   bets           — every tracked bet (open + settled).  Mirror of
--                    the local data/ledger.json open_bets + history.
--   bankroll       — per-sport current/starting balance, model +
--                    personal sides.
--   records        — per-sport-per-bet-type W/L records.
--   model_picks    — model's daily picks (analyzed_at, sport, game,
--                    each bet-type's pick prob / edge / value flag).
--                    Wired up in PR #18.
--   model_history  — every individual XGB / LR / NN pick across the
--                    whole tracking window.  Wired up in PR #19.
--
-- The standard columns the user-facing app needs are first-class;
-- a JSONB "meta" column on each table preserves the rest of the
-- existing ledger fields (model_prob, xgb_prob, lr_prob, nn_prob,
-- parlay_id, limit_reached, etc.) so nothing is lost in migration.
-- ============================================================

-- ──────────────────────────────────────────────────────────────
-- bets — individual tracked bets
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bets (
  id               TEXT        PRIMARY KEY,
  date             TEXT        NOT NULL,         -- YYYY-MM-DD (game date)
  sport            TEXT        NOT NULL,         -- 'mlb' | 'wnba' | ...
  bet_type         TEXT        NOT NULL,         -- 'single' | 'run_line' | 'spread' | 'totals' | 'parlay'
  teams            TEXT        NOT NULL,         -- 'Away @ Home' display string
  pick             TEXT        NOT NULL,         -- 'Yankees -1.5' / 'UNDER 8.5' / 'Lakers ML'
  odds             INTEGER,                      -- American odds
  dollar_amount    DOUBLE PRECISION,             -- amount staked ($)
  units            DOUBLE PRECISION,             -- units staked (1u = 1% of starting bankroll)
  confidence_tier  TEXT,                         -- 'strong' | 'moderate' | 'low' | 'split'
  edge_percentage  DOUBLE PRECISION,             -- pre-game edge as %, e.g. 5.2
  result           TEXT,                         -- 'win' | 'loss' | 'push' | NULL while open
  settled          BOOLEAN     NOT NULL DEFAULT FALSE,
  placed_at        TIMESTAMPTZ,
  settled_at       TIMESTAMPTZ,
  meta             JSONB,                        -- preserves home_team, away_team, model_prob, xgb/lr/nn_prob, parlay_id, limit_reached, etc.
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS bets_sport_date_idx     ON bets (sport, date DESC);
CREATE INDEX IF NOT EXISTS bets_settled_sport_idx  ON bets (settled, sport);
CREATE INDEX IF NOT EXISTS bets_placed_at_idx      ON bets (placed_at DESC);

-- ──────────────────────────────────────────────────────────────
-- bankroll — one row per sport
-- The pre-existing ledger tracks BOTH a model-bankroll (simulated
-- Kelly betting at full size) and a personal-bankroll (the user's
-- real-money side, half-Kelly).  Both pairs are stored so the
-- existing summary screens keep working unchanged.
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bankroll (
  sport             TEXT        PRIMARY KEY,
  current_balance   DOUBLE PRECISION NOT NULL,   -- alias of personal_current
  starting_balance  DOUBLE PRECISION NOT NULL,   -- alias of personal_starting
  model_current     DOUBLE PRECISION,
  model_starting    DOUBLE PRECISION,
  personal_current  DOUBLE PRECISION,
  personal_starting DOUBLE PRECISION,
  last_updated      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ──────────────────────────────────────────────────────────────
-- records — aggregated W/L per sport + bet_type
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS records (
  sport       TEXT             NOT NULL,
  bet_type    TEXT             NOT NULL,
  wins        INTEGER          NOT NULL DEFAULT 0,
  losses      INTEGER          NOT NULL DEFAULT 0,
  pushes      INTEGER          NOT NULL DEFAULT 0,
  units_won   DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  last_updated TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
  PRIMARY KEY (sport, bet_type)
);

-- ──────────────────────────────────────────────────────────────
-- model_picks — per-model performance tracking.  One row per
-- (model, game/prop, day) for every individual model (XGB/LR/NN per
-- bet type), the ensemble, and the user-facing consensus; results are
-- settled in the 15-minute cycle.  Dedup is on the UNIQUE
-- (date, game_id, model_name, pick_type) key (game_id holds the
-- player|market|line key for props so props are distinguished too).
--
-- NOTE: this replaces the earlier (unused) model_picks snapshot schema.
-- For an existing project that still has the old table, the app runs the
-- ADD COLUMN / DROP NOT NULL repair migration on startup (see
-- src/db.py:ensure_model_picks_schema), or run this block by hand.
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_picks (
  pick_id         UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  date            DATE,
  sport           TEXT,
  game_id         TEXT,
  prop_id         TEXT,
  model_name      TEXT,
  pick_type       TEXT,                           -- ML | RL | Spread | Total | <prop_market>
  pick_side       TEXT,                           -- home/away/over/under/Over/Under
  odds            INTEGER,
  confidence      DOUBLE PRECISION,
  projected_value DOUBLE PRECISION,
  line            DOUBLE PRECISION,
  result          TEXT         DEFAULT 'pending', -- pending | correct | incorrect | void
  settled_at      TIMESTAMPTZ,
  created_at      TIMESTAMPTZ  DEFAULT NOW(),
  CONSTRAINT model_picks_dedup_key UNIQUE (date, game_id, model_name, pick_type)
);
CREATE INDEX IF NOT EXISTS model_picks_date_model_idx ON model_picks (date DESC, model_name);

-- ──────────────────────────────────────────────────────────────
-- model_history — per-classifier (XGB / LR / NN) individual picks
-- across the entire tracking window.  Mirror of the existing
-- .cache/lr_picks_history.json, .cache/xgb_picks_history.json,
-- and data/nn_picks_history.json files.  Populated in PR #19.
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_history (
  id          BIGSERIAL    PRIMARY KEY,
  date        TEXT         NOT NULL,
  sport       TEXT         NOT NULL,
  bet_type    TEXT         NOT NULL,
  game_id     TEXT,
  model       TEXT         NOT NULL,            -- 'xgb' | 'lr' | 'nn'
  pick_team   TEXT,
  pick_side   TEXT,                              -- 'home' | 'away' | 'over' | 'under'
  pick_prob   DOUBLE PRECISION,
  result      TEXT,                              -- settled later
  meta        JSONB,
  created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  UNIQUE (game_id, bet_type, model)
);
CREATE INDEX IF NOT EXISTS model_history_date_sport_model_idx ON model_history (date DESC, sport, model);
