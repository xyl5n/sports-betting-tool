"""
Bet ledger: tracks open bets, settles via Odds API scores, and maintains
two parallel bankrolls:
  - model_bankroll    : every recommended bet (edge >= 3%) auto-logged
  - confirmed_bankroll: only bets the user explicitly confirmed

Bankroll accounting (stake-upfront model):
  - The stake is deducted from the bankroll immediately when a bet is placed.
  - On settlement:
      Win:  bankroll += stake × decimal  (returns stake + profit)
      Loss: nothing returned             (stake already deducted)
      Push: bankroll += stake            (returns stake only, no profit)
  - model_bankroll always shows the current AVAILABLE balance.
  - Amount at risk = sum of stakes in open (unsettled) bets.

Daily exposure cap:
  - Total model stakes placed on a given calendar day (UTC) may not exceed
    15% of starting_bankroll.
  - Bets that would breach this limit are recorded with amount = 0 and
    limit_reached = True. They appear in the ledger for visibility but have
    no financial impact.
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .kelly import american_to_decimal, size_bet

_logger = logging.getLogger(__name__)

# ── Per-model tracker settlers (imported lazily to avoid hard boot dependency) ─
# Each tracker exposes settle_<model>_pick(game_id, home_score, away_score).
# Missing trackers (e.g. nn not yet created) are silently skipped.
try:
    from .xgb_picks_tracker import settle_xgb_pick as _settle_xgb
except Exception as _e:  # pragma: no cover
    _logger.debug("xgb tracker unavailable: %s", _e)
    _settle_xgb = None  # type: ignore[assignment]

try:
    from .lr_picks_tracker import settle_lr_pick as _settle_lr
except Exception as _e:  # pragma: no cover
    _logger.debug("lr tracker unavailable: %s", _e)
    _settle_lr = None  # type: ignore[assignment]

try:
    from .nn_picks_tracker import settle_nn_pick as _settle_nn  # type: ignore[import]
except Exception as _e:  # pragma: no cover
    _logger.debug("nn tracker unavailable: %s", _e)
    _settle_nn = None  # type: ignore[assignment]


def _settle_model_trackers(game_id: str, home_score: int, away_score: int) -> None:
    """
    Propagate a finished game's scores to each individual-model tracker so
    their history files stay in sync with the main ledger settlement.

    Silently catches per-tracker failures — the main settlement flow must
    never be interrupted by a tracker error.
    """
    for tag, fn in (("xgb", _settle_xgb), ("lr", _settle_lr), ("nn", _settle_nn)):
        if fn is None:
            continue
        try:
            fn(str(game_id), int(home_score), int(away_score))
        except Exception as exc:
            _logger.warning("model tracker settle failed [%s] game=%s: %s", tag, game_id, exc)

DAILY_EXPOSURE_PCT = 0.50   # 50 % of starting bankroll per day (hard ceiling)

# ── Permanent archive (never cleared by reset) ────────────────────────────────
_ARCHIVE_PATH = Path("data/bet_history_archive.json")


def _append_to_archive(bets: list[dict]) -> None:
    """
    Append newly-settled bets to the permanent archive file.
    Deduplicates by bet ID so re-runs never double-count.
    Silently swallows errors so settlement itself is never disrupted.
    """
    if not bets:
        return
    try:
        if _ARCHIVE_PATH.exists():
            raw  = json.loads(_ARCHIVE_PATH.read_text(encoding="utf-8"))
            data = raw if isinstance(raw, dict) else {"bets": raw}
        else:
            data = {"bets": []}

        existing_ids = {b.get("id") for b in data.get("bets", []) if b.get("id")}
        new_bets     = [b for b in bets if b.get("id") not in existing_ids]
        if new_bets:
            data.setdefault("bets", []).extend(new_bets)
            _ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _ARCHIVE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[archive] write error: {exc}")


class Ledger:
    def __init__(self, path: str = "data/ledger.json", starting_bankroll: float = 1000.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._starting = starting_bankroll
        self.data = self._load()

    # ── persistence ───────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)

            # ── Migration: convert old single-bankroll format to split format ──
            # Old format had: starting_bankroll, model_bankroll, confirmed_bankroll
            # New format has: model_starting_bankroll, model_bankroll,
            #                 personal_starting_bankroll, personal_bankroll
            if "personal_bankroll" not in raw:
                # Carry over the old confirmed_bankroll value
                raw["personal_bankroll"] = raw.pop("confirmed_bankroll", self._starting)
            if "model_starting_bankroll" not in raw:
                raw["model_starting_bankroll"] = raw.get("starting_bankroll", 1000.0)
            if "personal_starting_bankroll" not in raw:
                raw["personal_starting_bankroll"] = raw.get("starting_bankroll", self._starting)
            # Remove old unified key now that it's been migrated
            raw.pop("starting_bankroll", None)

            # Ensure model_bankroll exists (defaults to 1000 if somehow missing)
            if "model_bankroll" not in raw:
                raw["model_bankroll"] = 1000.0

            return raw

        # Brand-new file — model always starts at $1,000; personal starts at
        # whatever the caller passed (e.g. the user's configured bankroll).
        return {
            "model_starting_bankroll":    1000.0,
            "model_bankroll":             1000.0,
            "personal_starting_bankroll": self._starting,
            "personal_bankroll":          self._starting,
            "open_bets":                  [],
            "history":                    [],
        }

    def save(self) -> None:
        # JSON write is the source of truth in-process; Supabase is a hot
        # backup when configured.  Both writes are best-effort: a Supabase
        # failure logs a warning and is swallowed so the app keeps running.
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        try:
            self._sync_to_supabase()
        except Exception as exc:                                              # noqa: BLE001
            _logger = logging.getLogger(__name__)
            _logger.warning("Ledger Supabase sync failed (JSON ok): %s", exc)

    def _sync_to_supabase(self) -> None:
        """Push the current ledger state to Supabase if connected.

        Idempotent — every save() pushes a full snapshot of open_bets +
        history + bankroll + records, so individual failures self-heal on
        the next save().  No-op when Supabase isn't configured.
        """
        try:
            from . import db
        except Exception:
            return
        if not db.is_supabase():
            return

        # Infer sport from the ledger file path (data/ledger.json → mlb,
        # data/wnba_ledger.json → wnba).  Per-sport ledger == per-sport
        # bankroll/records rows on the Supabase side.
        name = self.path.name.lower()
        sport = "wnba" if "wnba" in name else "mlb"

        # 1) Bets — both open and settled, stamped with the inferred sport
        bets_to_push = []
        for b in (self.data.get("open_bets") or []):
            bets_to_push.append({**b, "sport": b.get("sport") or sport})
        for b in (self.data.get("history") or []):
            bets_to_push.append({**b, "sport": b.get("sport") or sport})
        if bets_to_push:
            db.upsert_bets_bulk(bets_to_push)

        # 2) Bankroll — one row per sport, both model + personal sides
        db.upsert_bankroll(sport, {
            "current_balance":   self.data.get("personal_bankroll"),
            "starting_balance":  self.data.get("personal_starting_bankroll"),
            "model_current":     self.data.get("model_bankroll"),
            "model_starting":    self.data.get("model_starting_bankroll"),
            "personal_current":  self.data.get("personal_bankroll"),
            "personal_starting": self.data.get("personal_starting_bankroll"),
        })

        # 3) Records — aggregate W/L/Push per (sport, bet_type) from history
        records: dict[tuple[str, str], dict] = {}
        for b in (self.data.get("history") or []):
            bt = b.get("bet_type", "single")
            sp = (b.get("sport") or sport).lower()
            key = (sp, bt)
            rec = records.setdefault(key, {
                "sport":     sp,
                "bet_type":  bt,
                "wins":      0,
                "losses":    0,
                "pushes":    0,
                "units_won": 0.0,
            })
            result = (b.get("result") or "").lower()
            if   result == "win":  rec["wins"]   += 1
            elif result == "loss": rec["losses"] += 1
            elif result == "push": rec["pushes"] += 1
            rec["units_won"] += float(b.get("units_won") or 0.0)
        if records:
            db.upsert_records_bulk(list(records.values()))

    # ── helpers ───────────────────────────────────────────────────────────────

    def has_bet(self, game_id: str, bet_type: str = "single") -> bool:
        return any(
            b["game_id"] == game_id and b.get("bet_type", "single") == bet_type
            for b in self.data["open_bets"]
        )

    def kelly_amounts(self, model_prob: float, american_odds: int) -> tuple[float, float]:
        """Return (model_amount, personal_amount) sized off current ledger bankrolls."""
        model_starting    = self.data.get("model_starting_bankroll",    1000.0)
        personal_starting = self.data.get("personal_starting_bankroll", self._starting)
        _, m, _, _ = size_bet(model_prob, american_odds,
                               self.data["model_bankroll"], model_starting,
                               is_user_bet=False)
        _, c, _, _ = size_bet(model_prob, american_odds,
                               self.data["personal_bankroll"], personal_starting,
                               is_user_bet=True)
        return round(m, 2), round(c, 2)

    def _daily_exposure(self, today_str: str, confirmed_only: bool = False) -> float:
        """Total stake of open bets placed on today_str (YYYY-MM-DD)."""
        total = 0.0
        for b in self.data["open_bets"]:
            if b.get("limit_reached"):
                continue
            if b.get("placed_at", "")[:10] != today_str:
                continue
            if confirmed_only:
                total += b.get("confirmed_amount", 0.0)
            else:
                total += b.get("model_amount", 0.0)
        return total

    # ── write ─────────────────────────────────────────────────────────────────

    def add_bet(
        self,
        game: dict,
        sport: str,
        sport_key: str,
        side: str,          # "home"/"away" for ML/RL; "over"/"under" for totals
        team: str,
        odds: int,
        model_prob: float,
        edge: float,
        model_amount: float,
        confirmed: bool,
        confirmed_amount: float,
        bet_type: str = "single",
        parlay_id: str | None = None,
        parlay_name: str | None = None,
        prop_line: float | None = None,
        confidence_tier: str = "strong",
        xgb_prob: float | None = None,
        lr_prob: float | None = None,
        nn_prob: float | None = None,
    ) -> None:
        today          = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        model_starting = self.data.get("model_starting_bankroll", 1000.0)

        # Daily exposure cap (model bets only — uses model starting bankroll)
        limit_reached = False
        if not confirmed:
            daily_limit = model_starting * DAILY_EXPOSURE_PCT
            current     = self._daily_exposure(today, confirmed_only=False)
            if current + model_amount > daily_limit + 0.001:  # 0.001 float tolerance
                limit_reached = True
                model_amount  = 0.0

        # Immediately deduct stake from available balance
        if not limit_reached:
            self.data["model_bankroll"] = round(
                self.data["model_bankroll"] - model_amount, 2
            )
            if confirmed and confirmed_amount > 0:
                self.data["personal_bankroll"] = round(
                    self.data["personal_bankroll"] - confirmed_amount, 2
                )

        entry: dict = {
            "id":               str(uuid.uuid4()),
            "game_id":          game["id"],
            "sport":            sport,
            "sport_key":        sport_key,
            "home_team":        game["home_team"],
            "away_team":        game["away_team"],
            "bet_team":         team,
            "bet_side":         side,
            "american_odds":    odds,
            "commence_time":    game.get("commence_time", ""),
            "placed_at":        datetime.now(timezone.utc).isoformat(),
            "model_prob":       round(model_prob, 4),
            "edge":             round(edge, 4),
            "model_amount":     round(model_amount, 2),
            "confirmed":        confirmed,
            "confirmed_amount": round(confirmed_amount, 2) if confirmed else 0.0,
            "bet_type":         bet_type,
            "confidence_tier":  confidence_tier,
            "limit_reached":    limit_reached,
        }
        if parlay_id:
            entry["parlay_id"]   = parlay_id
        if parlay_name:
            entry["parlay_name"] = parlay_name
        if prop_line is not None:
            entry["prop_line"] = prop_line
        if xgb_prob is not None:
            entry["xgb_prob"] = round(xgb_prob, 4)
        if lr_prob is not None:
            entry["lr_prob"]  = round(lr_prob, 4)
        if nn_prob is not None:
            entry["nn_prob"]  = round(nn_prob, 4)

        self.data["open_bets"].append(entry)

    # ── settlement ────────────────────────────────────────────────────────────

    def settle(self, odds_client, sport_key: str) -> list[dict]:
        """
        Query Odds API scores for open bets matching sport_key and auto-settle them.
        Stake was already deducted at placement:
          Win  → return stake × decimal (stake + profit)
          Push → return stake only       (no profit, no loss)
          Loss → nothing returned
        """
        open_for_sport = [b for b in self.data["open_bets"] if b["sport_key"] == sport_key]
        if not open_for_sport:
            return []

        try:
            scores = odds_client.get_scores(sport_key=sport_key, days_from=3)
        except Exception as exc:                                              # noqa: BLE001
            _logger.warning(
                "settle: get_scores(%s) raised %s — %d open bets stay unsettled",
                sport_key, exc, len(open_for_sport),
            )
            return []

        if not scores:
            _logger.info(
                "settle: get_scores(%s) returned 0 rows; %d open bets stay open",
                sport_key, len(open_for_sport),
            )

        # ── Primary lookup: by Odds API game id ──────────────────────────────
        score_map = {s["id"]: s for s in scores if s.get("id")}

        # ── Fallback lookup: by (team-pair, date) ─────────────────────────────
        # The Odds API occasionally rotates game IDs between pre-game odds and
        # post-game scores.  When that happens the bet's stored game_id no
        # longer maps to a score row and the bet sits open forever.  Build a
        # secondary index keyed by the (away+home) team set so we can rescue
        # those orphans.
        def _norm(t: str | None) -> str:
            return (t or "").strip().lower()
        pair_map: dict[tuple[frozenset[str], str], dict] = {}
        for s in scores:
            teams: set[str] = set()
            ht, at = _norm(s.get("home_team")), _norm(s.get("away_team"))
            if ht: teams.add(ht)
            if at: teams.add(at)
            for nm in (s.get("scores") or []):
                n = _norm(nm.get("name") if isinstance(nm, dict) else None)
                if n: teams.add(n)
            if len(teams) < 2:
                continue
            date_str = (s.get("commence_time") or "")[:10]
            pair_map[(frozenset(teams), date_str)] = s

        newly_settled: list[dict] = []
        remaining:     list[dict] = []

        for bet in self.data["open_bets"]:
            if bet["sport_key"] != sport_key:
                remaining.append(bet)
                continue

            score = score_map.get(bet["game_id"])
            if score is None:
                # Fallback: same team pair on same date
                key = (
                    frozenset({_norm(bet.get("home_team")), _norm(bet.get("away_team"))}),
                    (bet.get("commence_time") or "")[:10],
                )
                score = pair_map.get(key)
                if score is not None:
                    _logger.info(
                        "settle: rescued bet %s via team-name fallback "
                        "(stored game_id=%s -> Odds API id=%s)",
                        bet.get("id"), bet.get("game_id"), score.get("id"),
                    )

            if score is None:
                _logger.debug(
                    "settle: no score row for bet %s (%s @ %s on %s) — leaving open",
                    bet.get("id"), bet.get("away_team"), bet.get("home_team"),
                    (bet.get("commence_time") or "")[:10],
                )
                remaining.append(bet)
                continue
            if not score.get("completed"):
                remaining.append(bet)
                continue
            if not score.get("scores"):
                _logger.warning(
                    "settle: bet %s game completed=True but no scores payload — leaving open",
                    bet.get("id"),
                )
                remaining.append(bet)
                continue

            # Use the matched score row's home_team (in case fallback rescued
            # from a different ID with slightly different team spelling).
            home = score.get("home_team") or bet.get("home_team")
            try:
                tally     = {s["name"]: int(float(s["score"])) for s in score["scores"]}
                away      = next(n for n in tally if n != home)
                home_runs = tally[home]
                away_runs = tally[away]
                margin    = home_runs - away_runs
            except Exception as exc:                                          # noqa: BLE001
                _logger.warning(
                    "settle: score-parse failure for bet %s (%s): %s",
                    bet.get("id"), score.get("scores"), exc,
                )
                remaining.append(bet)
                continue

            bet_type = bet.get("bet_type", "single")
            side     = bet["bet_side"]

            # ── Tri-state result: "win" | "loss" | "push" ─────────────────────
            # Pushes happen when the margin (run_line/spread) or total (totals)
            # lands exactly on the line.  Half-integer lines (.5) make pushes
            # impossible by design, but integer lines on WNBA spreads / totals
            # do hit -- they must return the stake, not silently flip to loss.
            if bet_type in ("run_line", "spread"):
                prop_line = bet.get("prop_line", 1.5)
                if   margin >  prop_line: result = "win"  if side == "home" else "loss"
                elif margin <  prop_line: result = "loss" if side == "home" else "win"
                else:                     result = "push"
            elif bet_type == "totals":
                prop_line = bet.get("prop_line", 8.5)
                total     = home_runs + away_runs
                if   total >  prop_line: result = "win"  if side == "over" else "loss"
                elif total <  prop_line: result = "loss" if side == "over" else "win"
                else:                    result = "push"
            else:
                # Moneyline — ties aren't possible in baseball / basketball.
                won = (margin > 0) == (side == "home")
                result = "win" if won else "loss"

            # ── Per-bet audit log: prints the inputs and the computed result
            #    to stderr so every settled bet leaves a verifiable trail.
            #    Reads like:
            #       SETTLEMENT-AUDIT [MLB]: ML home Yankees | Yankees 6 - 3 Red Sox
            #                              | margin=+3 -> WIN
            #       SETTLEMENT-AUDIT [MLB]: RL home Yankees | line=-1.5
            #                              | margin=+3 vs +1.5 -> WIN
            #       SETTLEMENT-AUDIT [MLB]: TOT over | line=8.5
            #                              | total=9 -> WIN
            try:
                import sys as _audit_sys
                _short_bt = {
                    "single":   "ML",
                    "run_line": "RL",
                    "spread":   "SPR",
                    "totals":   "TOT",
                }.get(bet_type, bet_type.upper())
                _sport_tag = (
                    "WNBA" if (bet.get("sport_key") or "").startswith("basketball")
                    else "MLB"
                )
                _away = bet.get("away_team", "?")
                _home = bet.get("home_team", "?")
                _team = bet.get("bet_team", "?")
                if bet_type == "totals":
                    _audit_sys.stderr.write(
                        f"SETTLEMENT-AUDIT [{_sport_tag}]: TOT {side} | "
                        f"line={bet.get('prop_line', 8.5)} | "
                        f"{_away} {away_runs} - {home_runs} {_home} "
                        f"total={home_runs + away_runs} -> {result.upper()}\n"
                    )
                elif bet_type in ("run_line", "spread"):
                    _line = bet.get("prop_line", 1.5)
                    _audit_sys.stderr.write(
                        f"SETTLEMENT-AUDIT [{_sport_tag}]: {_short_bt} "
                        f"{side} {_team} | line={'+' if _line >= 0 else ''}{_line} | "
                        f"{_away} {away_runs} - {home_runs} {_home}  "
                        f"margin={'+' if margin >= 0 else ''}{margin} "
                        f"vs {'+' if _line >= 0 else ''}{_line} -> {result.upper()}\n"
                    )
                else:
                    _audit_sys.stderr.write(
                        f"SETTLEMENT-AUDIT [{_sport_tag}]: ML {side} {_team} | "
                        f"{_away} {away_runs} - {home_runs} {_home}  "
                        f"margin={'+' if margin >= 0 else ''}{margin} -> "
                        f"{result.upper()}\n"
                    )
                _audit_sys.stderr.flush()
            except Exception:                                              # noqa: BLE001
                pass  # audit log must never break settlement

            decimal   = american_to_decimal(bet["american_odds"])
            model_amt = bet.get("model_amount", 0.0)
            conf_amt  = bet.get("confirmed_amount", 0.0)
            limit_hit = bet.get("limit_reached", False)

            # ── Model bankroll: win → stake*decimal; push → stake; loss → 0 ──
            if not limit_hit and model_amt > 0:
                if result == "win":
                    self.data["model_bankroll"] = round(
                        self.data["model_bankroll"] + model_amt * decimal, 2
                    )
                    model_pnl = round(model_amt * (decimal - 1), 2)
                elif result == "push":
                    self.data["model_bankroll"] = round(
                        self.data["model_bankroll"] + model_amt, 2
                    )
                    model_pnl = 0.0
                else:  # loss
                    model_pnl = -model_amt
            else:
                model_pnl = 0.0

            # ── Personal bankroll: same payout rules, only if confirmed ──────
            confirmed_pnl = 0.0
            if bet.get("confirmed") and not limit_hit and conf_amt > 0:
                if result == "win":
                    self.data["personal_bankroll"] = round(
                        self.data["personal_bankroll"] + conf_amt * decimal, 2
                    )
                    confirmed_pnl = round(conf_amt * (decimal - 1), 2)
                elif result == "push":
                    self.data["personal_bankroll"] = round(
                        self.data["personal_bankroll"] + conf_amt, 2
                    )
                    confirmed_pnl = 0.0
                else:  # loss
                    confirmed_pnl = -conf_amt

            settled = {
                **bet,
                "result":        result,
                "model_pnl":     model_pnl,
                "confirmed_pnl": confirmed_pnl,
                "settled_at":    datetime.now(timezone.utc).isoformat(),
            }
            self.data["history"].append(settled)
            newly_settled.append(settled)

            # Propagate final scores to per-model history trackers
            _settle_model_trackers(bet["game_id"], home_runs, away_runs)

        self.data["open_bets"] = remaining
        if newly_settled:
            self.save()
            _append_to_archive(newly_settled)
        return newly_settled

    def settle_manual(self, bet_id: str, result: str) -> dict | None:
        """
        Manually settle a single open bet.
        result: 'win' | 'loss' | 'push'
          Win:  return stake × decimal (stake + profit)
          Loss: nothing returned (stake already deducted at placement)
          Push: return stake only, no profit
        """
        bet       = None
        remaining = []
        for b in self.data["open_bets"]:
            if b["id"] == bet_id:
                bet = b
            else:
                remaining.append(b)

        if bet is None:
            return None

        decimal   = american_to_decimal(bet["american_odds"])
        model_amt = bet.get("model_amount", 0.0)
        conf_amt  = bet.get("confirmed_amount", 0.0)
        limit_hit = bet.get("limit_reached", False)

        # Model bankroll update
        if not limit_hit and model_amt > 0:
            if result == "win":
                self.data["model_bankroll"] = round(
                    self.data["model_bankroll"] + model_amt * decimal, 2
                )
                model_pnl = round(model_amt * (decimal - 1), 2)
            elif result == "push":
                self.data["model_bankroll"] = round(
                    self.data["model_bankroll"] + model_amt, 2
                )
                model_pnl = 0.0
            else:  # loss
                model_pnl = -model_amt
        else:
            model_pnl = 0.0

        # Personal bankroll update
        confirmed_pnl = 0.0
        if bet.get("confirmed") and not limit_hit and conf_amt > 0:
            if result == "win":
                self.data["personal_bankroll"] = round(
                    self.data["personal_bankroll"] + conf_amt * decimal, 2
                )
                confirmed_pnl = round(conf_amt * (decimal - 1), 2)
            elif result == "push":
                self.data["personal_bankroll"] = round(
                    self.data["personal_bankroll"] + conf_amt, 2
                )
                confirmed_pnl = 0.0
            else:  # loss
                confirmed_pnl = -conf_amt

        settled = {
            **bet,
            "result":        result,
            "model_pnl":     model_pnl,
            "confirmed_pnl": confirmed_pnl,
            "settled_at":    datetime.now(timezone.utc).isoformat(),
        }
        self.data["open_bets"] = remaining
        self.data["history"].append(settled)
        self.save()
        _append_to_archive([settled])
        return settled

    # ── summary ───────────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        history      = self.data["history"]
        model_wins   = sum(1 for h in history if h["result"] == "win")
        model_losses = sum(1 for h in history if h["result"] == "loss")
        model_pnl    = round(sum(h.get("model_pnl", 0) for h in history), 2)

        c_hist      = [h for h in history if h.get("confirmed")]
        conf_wins   = sum(1 for h in c_hist if h["result"] == "win")
        conf_losses = sum(1 for h in c_hist if h["result"] == "loss")
        conf_pnl    = round(sum(h.get("confirmed_pnl", 0) for h in c_hist), 2)

        # Stakes currently tied up in unsettled bets
        model_at_risk = round(sum(
            b.get("model_amount", 0.0)
            for b in self.data["open_bets"]
            if not b.get("limit_reached")
        ), 2)
        conf_at_risk = round(sum(
            b.get("confirmed_amount", 0.0)
            for b in self.data["open_bets"]
            if b.get("confirmed") and not b.get("limit_reached")
        ), 2)

        return {
            # Model bankroll fields
            "model_starting_bankroll":    self.data.get("model_starting_bankroll",    1000.0),
            "model_bankroll":             self.data["model_bankroll"],
            # Personal (user-confirmed) bankroll fields
            "personal_starting_bankroll": self.data.get("personal_starting_bankroll", self._starting),
            "personal_bankroll":          self.data["personal_bankroll"],
            # Records & P&L
            "model_record":       (model_wins, model_losses),
            "confirmed_record":   (conf_wins, conf_losses),
            "model_pnl":          model_pnl,
            "confirmed_pnl":      conf_pnl,
            "open_bets":          len(self.data["open_bets"]),
            "open_confirmed":     sum(1 for b in self.data["open_bets"] if b.get("confirmed")),
            "model_at_risk":      model_at_risk,
            "confirmed_at_risk":  conf_at_risk,
        }

    def get_model_weights(self, min_sample: int = 10) -> dict:
        """
        Compute per-model win rates from settled history bets that carry
        individual model probs (xgb_prob / lr_prob / nn_prob in the entry).
        Returns normalised weights summing to 1.0. Falls back to equal
        weights when fewer than min_sample settled bets exist per model.

        A model is "correct" when its stored probability (P that the bet
        side wins) was >= 0.5 and the bet won, or < 0.5 and the bet lost.
        Since we only record bets we made (model_prob >= 0.5 by design),
        "correct" simply means the bet won.
        """
        counts: dict[str, list[int]] = {m: [0, 0] for m in ("xgb", "lr", "nn")}
        for h in self.data["history"]:
            if h.get("result") not in ("win", "loss"):
                continue
            won = h["result"] == "win"
            for model, key in (("xgb", "xgb_prob"), ("lr", "lr_prob"), ("nn", "nn_prob")):
                p = h.get(key)
                if p is not None:
                    counts[model][1] += 1
                    if (float(p) >= 0.5) == won:
                        counts[model][0] += 1

        rates: dict[str, float] = {}
        for m, (correct, total) in counts.items():
            if total >= min_sample:
                rates[m] = correct / total

        using_real = len(rates) >= 2
        weights: dict[str, float]

        if not using_real:
            weights = {"xgb": 1 / 3, "lr": 1 / 3, "nn": 1 / 3}
        elif "nn" not in rates:
            xr = rates.get("xgb", 0.5)
            lr = rates.get("lr",  0.5)
            t  = xr + lr or 1.0
            weights = {"xgb": xr / t, "lr": lr / t, "nn": 0.0}
        else:
            t = sum(rates.values()) or 1.0
            weights = {m: rates.get(m, 0.0) / t for m in ("xgb", "lr", "nn")}

        return {
            "xgb":                weights["xgb"],
            "lr":                 weights["lr"],
            "nn":                 weights["nn"],
            "using_real_weights": using_real,
            "xgb_win_rate":       rates.get("xgb"),
            "lr_win_rate":        rates.get("lr"),
            "nn_win_rate":        rates.get("nn"),
            "xgb_sample":         counts["xgb"][1],
            "lr_sample":          counts["lr"][1],
            "nn_sample":          counts["nn"][1],
        }

    def is_active(self) -> bool:
        return bool(self.data["history"] or self.data["open_bets"])
