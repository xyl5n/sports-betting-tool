-- ============================================================
-- ⚠️ DEPRECATED — DO NOT RUN.  Kept for history only.
--
-- The LIVE model_picks schema is defined by
--   db/migrations/2026_model_picks_restructure.sql
-- which the application code actually reads/writes
-- (pick_id text, model, bet_type, status, result, confidence,
--  player_name).  src/db.py:ensure_model_picks_schema probes for
-- exactly those columns.
--
-- The column vocabulary BELOW (model_name, pick_type, prop_id,
-- odds, projected_value, date, uuid pick_id) is NOT referenced
-- anywhere in app/src/ or app/pages/ — it is dead weight from an
-- earlier repair attempt.  Running it against a live restructured
-- table only adds unused nullable columns (its CREATE TABLE is a
-- no-op once the table exists, and its uuid pick_id default never
-- applies because the table already uses a text pick_id).  Nothing
-- in the app auto-runs this file; it is retained purely as a record
-- of the legacy repair.
--
-- The original (now-superseded) header follows:
--
-- Migration: model_picks -> per-model performance schema.
--
-- Fixes the "Could not find the 'confidence' column" /
-- "column model_picks.result does not exist" insert errors by
-- repairing an existing/legacy model_picks table (the old
-- id/bet_type/teams/pick snapshot schema) in place.
--
-- Idempotent + safe to re-run.  Run once in the Supabase SQL
-- editor (project -> SQL -> New query -> paste -> Run).  The app
-- also attempts this automatically on startup when a SQL-exec RPC
-- is available (src/db.py:ensure_model_picks_schema).
-- ============================================================

CREATE TABLE IF NOT EXISTS public.model_picks (
  pick_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  date            date,
  sport           text,
  game_id         text,
  prop_id         text,
  model_name      text,
  pick_type       text,
  pick_side       text,
  odds            integer,
  confidence      double precision,
  projected_value double precision,
  line            double precision,
  result          text DEFAULT 'pending',
  settled_at      timestamptz,
  created_at      timestamptz DEFAULT now()
);

-- Backfill any columns missing from an older/partial table.
ALTER TABLE public.model_picks ADD COLUMN IF NOT EXISTS prop_id text;
ALTER TABLE public.model_picks ADD COLUMN IF NOT EXISTS model_name text;
ALTER TABLE public.model_picks ADD COLUMN IF NOT EXISTS pick_type text;
ALTER TABLE public.model_picks ADD COLUMN IF NOT EXISTS pick_side text;
ALTER TABLE public.model_picks ADD COLUMN IF NOT EXISTS odds integer;
ALTER TABLE public.model_picks ADD COLUMN IF NOT EXISTS confidence double precision;
ALTER TABLE public.model_picks ADD COLUMN IF NOT EXISTS projected_value double precision;
ALTER TABLE public.model_picks ADD COLUMN IF NOT EXISTS line double precision;
ALTER TABLE public.model_picks ADD COLUMN IF NOT EXISTS result text DEFAULT 'pending';
ALTER TABLE public.model_picks ADD COLUMN IF NOT EXISTS settled_at timestamptz;
ALTER TABLE public.model_picks ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();
ALTER TABLE public.model_picks ADD COLUMN IF NOT EXISTS sport text;
ALTER TABLE public.model_picks ADD COLUMN IF NOT EXISTS game_id text;
ALTER TABLE public.model_picks ADD COLUMN IF NOT EXISTS date date;

-- Legacy NOT NULL columns would reject the new inserts -> relax them.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name='model_picks' AND column_name='bet_type') THEN
    ALTER TABLE public.model_picks ALTER COLUMN bet_type DROP NOT NULL;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name='model_picks' AND column_name='teams') THEN
    ALTER TABLE public.model_picks ALTER COLUMN teams DROP NOT NULL;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name='model_picks' AND column_name='pick') THEN
    ALTER TABLE public.model_picks ALTER COLUMN pick DROP NOT NULL;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='model_picks_dedup_key') THEN
    ALTER TABLE public.model_picks
      ADD CONSTRAINT model_picks_dedup_key
      UNIQUE (date, game_id, model_name, pick_type);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS model_picks_date_model_idx
  ON public.model_picks (date DESC, model_name);
