"""
AI Strategy Engine for Road Worx Autonomous Betting.

Conservative strategy optimised for small, consistent wins (KES).
Uses statistical analysis, Kelly Criterion (fractional), mean-reversion
signals, and streak detection to decide: BET / SKIP / STOP.
"""

import os
import math
import json
import uuid
import logging
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AIConfig:
    """User-configurable parameters for the AI engine."""
    bankroll: float = 100.0               # Starting balance (KES)
    risk_level: str = "conservative"      # conservative | moderate | aggressive
    max_bet_pct: float = 5.0              # Max bet as % of current balance
    max_bet_abs: float = 50.0             # Absolute max bet (KES)
    stop_loss_pct: float = 30.0           # Stop session if loss exceeds % of bankroll
    take_profit_pct: float = 50.0         # Stop session if profit exceeds % of bankroll
    kelly_fraction: float = 0.25          # Fractional Kelly (0.25 = quarter Kelly)
    min_data_points: int = 30             # Minimum rounds before AI bets
    analysis_window: int = 100            # How many recent rounds to analyse
    cooldown_after_loss: int = 1           # Rounds to skip after a loss
    max_consecutive_losses: int = 5       # Force stop after N consecutive losses
    dry_run: bool = True                  # If True, log decisions but don't execute
    target_win_rate: float = 0.65         # Aim for this win probability
    preferred_targets: List[float] = field(
        default_factory=lambda: [1.25, 1.30, 1.50, 1.80, 1.95]
    )

    # Dynamic/Tunable weights and thresholds for evaluation
    w_prob_high: float = 15.0
    w_prob_med: float = 8.0
    w_mr_oversold: float = 10.0
    w_vol_low: float = 5.0
    w_streak_low: float = 8.0
    w_data_quality: float = 5.0
    w_mr_overbought: float = 12.0
    w_vol_high: float = 10.0
    w_streak_high: float = 8.0
    w_prob_low: float = 15.0
    w_loss_penalty: float = 5.0
    confidence_threshold: float = 55.0  # Dynamic threshold
    weight_ev: float = 0.4              # EV weight in target selection
    weight_prob: float = 0.6            # Probability weight in target selection

    def __post_init__(self):
        # Set default weights and thresholds based on risk level if they are unchanged from baseline defaults
        if self.confidence_threshold == 55.0 and self.weight_ev == 0.4 and self.weight_prob == 0.6:
            if self.risk_level == "moderate":
                self.confidence_threshold = 45.0
                self.weight_ev = 0.6
                self.weight_prob = 0.4
            elif self.risk_level == "aggressive":
                self.confidence_threshold = 35.0
                self.weight_ev = 0.8
                self.weight_prob = 0.2

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AIConfig":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


def get_optimized_params_path() -> str:
    """Helper to locate or create the optimized_params.json file in the instance directory."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    instance_dir = os.path.join(base_dir, "instance")
    os.makedirs(instance_dir, exist_ok=True)
    return os.path.join(instance_dir, "optimized_params.json")

def load_optimized_params() -> dict:
    """Loads optimized parameters from disk if they exist."""
    path = get_optimized_params_path()
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load optimized parameters: {e}")
    return {}

def save_optimized_params(params: dict):
    """Saves optimized parameters to disk."""
    path = get_optimized_params_path()
    try:
        with open(path, "w") as f:
            json.dump(params, f, indent=4)
        logger.info(f"Saved optimized parameters to {path}")
    except Exception as e:
        logger.error(f"Failed to save optimized parameters: {e}")


@dataclass
class AIDecision:
    """Output of a single AI decision cycle."""
    action: str                          # 'bet' | 'skip' | 'stop_session'
    stake: float = 0.0
    target_multiplier: float = 1.50
    confidence: float = 0.0              # 0-100
    reasoning: str = ""
    risk_level: str = "low"              # low | medium | high
    analysis: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SessionStats:
    """Running stats for the current AI session."""
    session_id: str = ""
    started_at: str = ""
    total_bets: int = 0
    wins: int = 0
    losses: int = 0
    skips: int = 0
    current_balance: float = 0.0
    starting_balance: float = 0.0
    peak_balance: float = 0.0
    lowest_balance: float = 0.0
    total_profit_loss: float = 0.0
    win_rate: float = 0.0
    roi: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    current_streak: int = 0              # +N wins, -N losses
    longest_win_streak: int = 0
    longest_loss_streak: int = 0
    consecutive_losses: int = 0
    rounds_since_last_bet: int = 0
    equity_curve: List[float] = field(default_factory=list)
    is_running: bool = False
    is_dry_run: bool = True

    def to_dict(self) -> dict:
        d = asdict(self)
        # Trim equity curve for transfer
        if len(d["equity_curve"]) > 200:
            step = max(1, len(d["equity_curve"]) // 200)
            d["equity_curve"] = d["equity_curve"][::step]
        return d


# ---------------------------------------------------------------------------
# AI Strategy Engine
# ---------------------------------------------------------------------------

class AIStrategyEngine:
    """
    Stateful engine that analyses historical multiplier data and produces
    betting decisions each round.
    """

    def __init__(self, db_session_factory, config: Optional[AIConfig] = None, shared_cache: Optional[dict] = None):
        self.db = db_session_factory
        self.config = config or AIConfig()
        self.stats = SessionStats()
        self._last_decision: Optional[AIDecision] = None
        self._analysis_cache: dict = {}
        self.shared_cache = shared_cache if shared_cache is not None else {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_session(self, config: Optional[AIConfig] = None):
        """Initialise a new trading session."""
        if config:
            self.config = config

        self.stats = SessionStats(
            session_id=str(uuid.uuid4()),
            started_at=datetime.utcnow().isoformat(),
            current_balance=self.config.bankroll,
            starting_balance=self.config.bankroll,
            peak_balance=self.config.bankroll,
            lowest_balance=self.config.bankroll,
            is_running=True,
            is_dry_run=self.config.dry_run,
        )
        self.stats.equity_curve.append(self.config.bankroll)
        logger.info(
            f"AI Session started | bankroll={self.config.bankroll} KES | "
            f"risk={self.config.risk_level} | dry_run={self.config.dry_run}"
        )

    def stop_session(self, reason: str = "user_stop"):
        """Gracefully end the session."""
        self.stats.is_running = False
        logger.info(f"AI Session stopped | reason={reason} | P/L={self.stats.total_profit_loss:.2f} KES")

    def make_decision(self, multipliers: List[float]) -> AIDecision:
        """
        Core decision function.  Accepts a list of recent multipliers
        (newest-first) and returns an AIDecision.
        """
        # Guard: not enough data. If so, perform bootstrap exploration bets to collect data!
        if len(multipliers) < self.config.min_data_points:
            if self.stats.current_balance >= 10:
                logger.info(f"Insufficient data ({len(multipliers)}/{self.config.min_data_points} rounds). Placing a bootstrap exploration bet.")
                return AIDecision(
                    action="bet",
                    stake=10.0,
                    target_multiplier=1.20,
                    confidence=50.0,
                    reasoning=f"Bootstrapping data collection ({len(multipliers)}/{self.config.min_data_points} rounds)",
                    risk_level="low",
                    analysis={"data_points": len(multipliers), "bootstrapping": True}
                )
            else:
                return self._skip(
                    f"Insufficient data ({len(multipliers)}/{self.config.min_data_points} rounds) and insufficient balance for minimum bet",
                    {"data_points": len(multipliers)},
                )

        # Guard: session limits
        limit_decision = self._check_session_limits()
        if limit_decision:
            return limit_decision

        # Guard: cooldown after loss
        if self.stats.consecutive_losses > 0 and self.stats.rounds_since_last_bet < self.config.cooldown_after_loss:
            self.stats.rounds_since_last_bet += 1
            return self._skip(
                f"Cooldown after loss ({self.stats.rounds_since_last_bet}/{self.config.cooldown_after_loss})",
                {},
            )

        # ---- Full analysis ----
        analysis = self._analyse(multipliers)
        self._analysis_cache = analysis

        # ---- Pick target multiplier ----
        target, target_prob = self._select_target(analysis)

        # ---- Kelly stake sizing ----
        stake = self._calculate_stake(target, target_prob, analysis)

        # ---- Bet / skip logic ----
        decision = self._evaluate(analysis, target, target_prob, stake)
        self._last_decision = decision
        return decision

    def record_outcome(self, actual_multiplier: float, decision: AIDecision):
        """
        Called after a round resolves.  Updates session stats.
        """
        if decision.action != "bet":
            self.stats.skips += 1
            return

        self.stats.total_bets += 1
        won = actual_multiplier >= decision.target_multiplier
        payout = decision.stake * decision.target_multiplier if won else 0
        profit = payout - decision.stake

        self.stats.current_balance += profit
        self.stats.total_profit_loss = self.stats.current_balance - self.stats.starting_balance
        self.stats.peak_balance = max(self.stats.peak_balance, self.stats.current_balance)
        self.stats.lowest_balance = min(self.stats.lowest_balance, self.stats.current_balance)
        self.stats.equity_curve.append(round(self.stats.current_balance, 2))

        if won:
            self.stats.wins += 1
            self.stats.consecutive_losses = 0
            self.stats.current_streak = max(1, self.stats.current_streak + 1) if self.stats.current_streak >= 0 else 1
            self.stats.longest_win_streak = max(self.stats.longest_win_streak, self.stats.current_streak)
            self.stats.best_trade = max(self.stats.best_trade, profit)
        else:
            self.stats.losses += 1
            self.stats.consecutive_losses += 1
            self.stats.current_streak = min(-1, self.stats.current_streak - 1) if self.stats.current_streak <= 0 else -1
            self.stats.longest_loss_streak = max(self.stats.longest_loss_streak, abs(self.stats.current_streak))
            self.stats.worst_trade = min(self.stats.worst_trade, profit)

        total = self.stats.wins + self.stats.losses
        self.stats.win_rate = round((self.stats.wins / total) * 100, 1) if total > 0 else 0
        self.stats.roi = round((self.stats.total_profit_loss / self.stats.starting_balance) * 100, 1)
        self.stats.rounds_since_last_bet = 0

        outcome = "WIN" if won else "LOSS"
        logger.info(
            f"AI Trade {outcome}: stake={decision.stake:.2f} target={decision.target_multiplier}x "
            f"actual={actual_multiplier:.2f}x P/L={profit:+.2f} KES  balance={self.stats.current_balance:.2f}"
        )

    def get_latest_analysis(self) -> dict:
        return self._analysis_cache

    def get_latest_decision(self) -> Optional[AIDecision]:
        return self._last_decision

    # ------------------------------------------------------------------
    # Internal analysis
    # ------------------------------------------------------------------

    def _analyse(self, multipliers: List[float]) -> dict:
        """Run full statistical analysis on the multiplier history."""
        # Convert window to tuple to use as cache key
        window_tuple = tuple(multipliers[:self.config.analysis_window])
        if window_tuple in self.shared_cache:
            return self.shared_cache[window_tuple]

        window = list(window_tuple)
        n = len(window)

        mean = statistics.mean(window)
        median = statistics.median(window)
        stdev = statistics.stdev(window) if n > 1 else 0
        variance = statistics.variance(window) if n > 1 else 0

        # Recent sub-windows
        r5 = window[:5] if n >= 5 else window
        r10 = window[:10] if n >= 10 else window
        r20 = window[:20] if n >= 20 else window
        mean_5 = statistics.mean(r5)
        mean_10 = statistics.mean(r10)
        mean_20 = statistics.mean(r20)

        # Distribution buckets
        buckets = {
            "1.00-1.50": 0, "1.51-2.00": 0, "2.01-3.00": 0,
            "3.01-5.00": 0, "5.01-10.00": 0, "10.00+": 0,
        }
        for m in window:
            if m <= 1.50:
                buckets["1.00-1.50"] += 1
            elif m <= 2.00:
                buckets["1.51-2.00"] += 1
            elif m <= 3.00:
                buckets["2.01-3.00"] += 1
            elif m <= 5.00:
                buckets["3.01-5.00"] += 1
            elif m <= 10.00:
                buckets["5.01-10.00"] += 1
            else:
                buckets["10.00+"] += 1

        bucket_pcts = {k: round(v / n * 100, 1) for k, v in buckets.items()}

        # Probability of reaching specific multipliers
        prob_targets = {}
        for t in [1.20, 1.25, 1.30, 1.50, 1.80, 1.95, 2.00, 3.00, 5.00]:
            hits = sum(1 for m in window if m >= t)
            prob_targets[f"{t}x"] = round(hits / n, 4)

        # Streaks
        streaks = self._calc_streaks(window)

        # Volatility (rolling stdev of last 10)
        vol_10 = statistics.stdev(r10) if len(r10) > 1 else 0
        vol_20 = statistics.stdev(r20) if len(r20) > 1 else 0

        # Mean reversion signal
        mr_signal = self._mean_reversion_signal(mean, mean_5, mean_10, stdev)

        # Skewness approximation
        skewness = 0.0
        if stdev > 0 and n > 2:
            skewness = sum((m - mean) ** 3 for m in window) / (n * stdev ** 3)

        result = {
            "data_points": n,
            "mean": round(mean, 3),
            "median": round(median, 3),
            "stdev": round(stdev, 3),
            "variance": round(variance, 3),
            "skewness": round(skewness, 3),
            "mean_5": round(mean_5, 3),
            "mean_10": round(mean_10, 3),
            "mean_20": round(mean_20, 3),
            "distribution": bucket_pcts,
            "distribution_counts": buckets,
            "probabilities": prob_targets,
            "streaks": streaks,
            "volatility_10": round(vol_10, 3),
            "volatility_20": round(vol_20, 3),
            "mean_reversion": mr_signal,
            "last_5": [round(m, 2) for m in r5],
        }
        self.shared_cache[window_tuple] = result
        return result

    def _calc_streaks(self, multipliers: List[float]) -> dict:
        """Analyse low/high streaks in the data."""
        current_type = None
        current_len = 0
        max_low = 0
        max_high = 0

        for m in multipliers:
            t = "high" if m >= 2.0 else "low"
            if t == current_type:
                current_len += 1
            else:
                if current_type == "low":
                    max_low = max(max_low, current_len)
                elif current_type == "high":
                    max_high = max(max_high, current_len)
                current_type = t
                current_len = 1

        # Final streak
        if current_type == "low":
            max_low = max(max_low, current_len)
        elif current_type == "high":
            max_high = max(max_high, current_len)

        # Current streak (from most recent)
        if not multipliers:
            return {"current_type": None, "current_length": 0, "max_low": 0, "max_high": 0}

        first_type = "high" if multipliers[0] >= 2.0 else "low"
        streak_len = 1
        for m in multipliers[1:]:
            t = "high" if m >= 2.0 else "low"
            if t == first_type:
                streak_len += 1
            else:
                break

        return {
            "current_type": first_type,
            "current_length": streak_len,
            "max_low": max_low,
            "max_high": max_high,
        }

    def _mean_reversion_signal(self, mean: float, mean_5: float, mean_10: float, stdev: float) -> dict:
        """
        Calculate how far recent averages deviate from long-term mean.
        Returns a signal: 'oversold' (expect recovery), 'overbought' (expect dip),
        or 'neutral'.
        """
        if stdev == 0:
            return {"signal": "neutral", "strength": 0, "deviation": 0}

        deviation = (mean_5 - mean) / stdev
        strength = min(100, abs(deviation) * 33)

        if deviation < -0.5:
            signal = "oversold"  # recent avg below long-term → expect recovery
        elif deviation > 0.5:
            signal = "overbought"  # recent avg above long-term → expect cooldown
        else:
            signal = "neutral"

        return {
            "signal": signal,
            "strength": round(strength, 1),
            "deviation": round(deviation, 3),
        }

    # ------------------------------------------------------------------
    # Target selection
    # ------------------------------------------------------------------

    def _select_target(self, analysis: dict) -> tuple:
        """
        Pick the optimal cashout target based on probability analysis
        and the configured risk level, dynamically adjusting it based on performance.
        """
        probs = analysis["probabilities"]
        mr = analysis["mean_reversion"]
        vol = analysis.get("volatility_10", 0)
        stdev = analysis.get("stdev", 1.0)

        best_target = 1.50
        best_ev = -999.0

        for target in self.config.preferred_targets:
            # Cap preferred targets at 1.95 max
            target = min(target, 1.95)
            key = f"{target}x"
            prob = probs.get(key, 0)
            ev = prob * (target - 1) - (1 - prob)
            score = ev * self.config.weight_ev + prob * self.config.weight_prob

            if score > best_ev:
                best_ev = score
                best_target = target

        # Dynamic Adjustment: AI decides target scaling based on streak and volatility
        # 1. Win streak scaling: Increase target on positive momentum
        if self.stats.current_streak > 0:
            # Scale target up by +0.05 per win, up to a maximum of 1.95x
            best_target = min(1.95, best_target + (self.stats.current_streak * 0.05))
            
        # 2. Loss streak scaling: Decrease target to increase safety
        elif self.stats.consecutive_losses > 0:
            # Scale target down by -0.05 per consecutive loss, down to a minimum of 1.20x
            best_target = max(1.20, best_target - (self.stats.consecutive_losses * 0.05))

        # 3. Mean reversion scaling
        if mr["signal"] == "oversold" and mr["strength"] > 40:
            best_target = min(1.95, best_target + 0.1)
        elif mr["signal"] == "overbought" and mr["strength"] > 40:
            best_target = max(1.20, best_target - 0.1)

        # 4. Volatility scaling
        if vol > stdev * 1.3:
            # Lower target under high volatility
            best_target = max(1.20, best_target - 0.1)
        elif vol < stdev * 0.7:
            # Raise target under low volatility
            best_target = min(1.95, best_target + 0.05)

        # Final rounding and clamping (Max 1.95x target absolute cap)
        best_target = round(best_target, 2)
        best_target = max(1.20, min(1.95, best_target))

        # Retrieve target probability
        prob_key = f"{best_target}x"
        if prob_key in probs:
            target_prob = probs[prob_key]
        else:
            # Interpolation/fallback if target is rounded/adjusted
            keys = [float(k.replace('x', '')) for k in probs.keys() if 'x' in k]
            keys.sort()
            if not keys:
                target_prob = 0.5
            else:
                closest_key = min(keys, key=lambda x: abs(x - best_target))
                target_prob = probs.get(f"{closest_key}x", 0.5)

        return best_target, target_prob

    # ------------------------------------------------------------------
    # Stake calculation (Kelly Criterion)
    # ------------------------------------------------------------------

    def _calculate_stake(self, target: float, win_prob: float, analysis: dict) -> float:
        """
        Use fractional Kelly Criterion to calculate optimal stake.
        f* = (bp - q) / b   where b = target-1, p = win_prob, q = 1-p
        Then apply fraction and caps.
        If Kelly Criterion is <= 0 (negative/zero edge), fall back to a small
        risk-adjusted flat size so the AI can actually play.
        """
        b = target - 1.0  # net odds
        p = win_prob
        q = 1 - p

        if b <= 0 or p <= 0:
            return 0

        kelly = (b * p - q) / b
        
        if kelly <= 0:
            # Fallback flat size based on risk level
            risk_pct = {"conservative": 1.0, "moderate": 2.0, "aggressive": 5.0}
            flat_pct = risk_pct.get(self.config.risk_level, 1.0)
            stake_pct = min(flat_pct, self.config.max_bet_pct)
        else:
            # Apply fractional Kelly
            stake_pct = kelly * self.config.kelly_fraction * 100

        # AI decides to increase/decrease the stake based on streak and volatility:
        # 1. Winning streak boost: scale up stake on a winning streak (minimum 2 consecutive wins)
        if self.stats.current_streak >= 2:
            # Scale up by +15% per consecutive win past the first one, capped at +60% max boost
            scale_up = 1.0 + min(0.60, (self.stats.current_streak - 1) * 0.15)
            stake_pct *= scale_up

        # 2. Reducing streak scale-down: Halve the stake after consecutive losses
        if self.stats.consecutive_losses > 0:
            reduction = 0.5 ** self.stats.consecutive_losses  # halve each loss
            stake_pct *= reduction

        # 3. Market Volatility check: Scale down stake on high volatility
        vol = analysis.get("volatility_10", 0)
        stdev = analysis.get("stdev", 1.0)
        if vol > stdev * 1.3:
            # Erratic market: reduce stake size by 25% for safety
            stake_pct *= 0.75

        # Cap at max bet percentage
        stake_pct = min(stake_pct, self.config.max_bet_pct)

        # Calculate absolute stake
        stake = self.stats.current_balance * (stake_pct / 100)

        # Apply absolute cap
        stake = min(stake, self.config.max_bet_abs)

        # Minimum bet (platform minimum is usually 10 KES)
        stake = max(10.0, stake) if stake > 0 else 0

        # Don't bet more than balance
        stake = min(stake, self.stats.current_balance)

        return round(stake, 2)

    # ------------------------------------------------------------------
    # Decision evaluation
    # ------------------------------------------------------------------

    def _evaluate(self, analysis: dict, target: float, target_prob: float, stake: float) -> AIDecision:
        """
        Final evaluation: should we bet, skip, or stop?
        """
        reasons = []
        confidence = 50.0
        risk = "low"

        mr = analysis["mean_reversion"]
        vol = analysis["volatility_10"]
        streaks = analysis["streaks"]
        mean = analysis["mean"]

        # --- Positive signals (increase confidence) ---

        # High probability target
        if target_prob >= 0.65:
            confidence += self.config.w_prob_high
            reasons.append(f"High probability target ({target}x @ {target_prob*100:.0f}%)")
        elif target_prob >= 0.55:
            confidence += self.config.w_prob_med
            reasons.append(f"Moderate probability target ({target}x @ {target_prob*100:.0f}%)")

        # Mean reversion: oversold → good time to bet
        if mr["signal"] == "oversold" and mr["strength"] > 30:
            confidence += self.config.w_mr_oversold
            reasons.append(f"Mean reversion: oversold (strength {mr['strength']:.0f}%)")

        # Low recent volatility
        if vol < analysis["stdev"] * 0.8:
            confidence += self.config.w_vol_low
            reasons.append("Low recent volatility")

        # After a low streak, recovery expected
        if streaks["current_type"] == "low" and streaks["current_length"] >= 3:
            confidence += self.config.w_streak_low
            reasons.append(f"Low streak ({streaks['current_length']} rounds) — recovery likely")

        # Good data quality
        if analysis["data_points"] >= 80:
            confidence += self.config.w_data_quality

        # --- Negative signals (decrease confidence) ---

        # Overbought / hot streak
        if mr["signal"] == "overbought" and mr["strength"] > 30:
            confidence -= self.config.w_mr_overbought
            reasons.append(f"Mean reversion: overbought — potential correction")

        # High volatility
        if vol > analysis["stdev"] * 1.5:
            confidence -= self.config.w_vol_high
            risk = "high"
            reasons.append("High recent volatility — unpredictable")

        # High streak (might reverse)
        if streaks["current_type"] == "high" and streaks["current_length"] >= 5:
            confidence -= self.config.w_streak_high
            reasons.append(f"Extended high streak ({streaks['current_length']}) — reversion risk")

        # Low probability target
        if target_prob < 0.45:
            confidence -= self.config.w_prob_low
            risk = "high"
            reasons.append(f"Low win probability ({target_prob*100:.0f}%)")

        # Consecutive losses
        if self.stats.consecutive_losses >= 2:
            confidence -= self.config.w_loss_penalty * self.stats.consecutive_losses
            reasons.append(f"Consecutive losses: {self.stats.consecutive_losses}")

        # Clamp confidence
        confidence = max(5, min(95, confidence))

        # Determine risk level
        if confidence >= 65:
            risk = "low"
        elif confidence >= 45:
            risk = "medium"
        else:
            risk = "high"

        # --- Final decision ---
        threshold = self.config.confidence_threshold

        if stake <= 0 or self.stats.current_balance < 10:
            return self._skip("Insufficient balance for minimum bet", analysis)

        if confidence < threshold:
            return self._skip(
                f"Confidence too low ({confidence:.0f}% < {threshold}% threshold). " +
                "; ".join(reasons[-2:]) if reasons else "Waiting for better conditions",
                analysis,
            )

        return AIDecision(
            action="bet",
            stake=stake,
            target_multiplier=target,
            confidence=round(confidence, 1),
            reasoning="; ".join(reasons) if reasons else "Conditions favorable for conservative bet",
            risk_level=risk,
            analysis=analysis,
        )

    # ------------------------------------------------------------------
    # Session limit checks
    # ------------------------------------------------------------------

    def _check_session_limits(self) -> Optional[AIDecision]:
        """Check if any session limits have been hit."""
        # Stop-loss
        loss_pct = abs(self.stats.total_profit_loss) / self.stats.starting_balance * 100 if self.stats.total_profit_loss < 0 else 0
        if loss_pct >= self.config.stop_loss_pct:
            self.stats.is_running = False
            return AIDecision(
                action="stop_session",
                reasoning=f"Stop-loss triggered: lost {loss_pct:.1f}% of bankroll (limit: {self.config.stop_loss_pct}%)",
                confidence=100,
                risk_level="high",
            )

        # Take profit
        profit_pct = (self.stats.total_profit_loss / self.stats.starting_balance * 100) if self.stats.total_profit_loss > 0 else 0
        if profit_pct >= self.config.take_profit_pct:
            self.stats.is_running = False
            return AIDecision(
                action="stop_session",
                reasoning=f"Take-profit target reached: +{profit_pct:.1f}% (target: {self.config.take_profit_pct}%)",
                confidence=100,
                risk_level="low",
            )

        # Max consecutive losses
        if self.stats.consecutive_losses >= self.config.max_consecutive_losses:
            self.stats.is_running = False
            return AIDecision(
                action="stop_session",
                reasoning=f"Max consecutive losses reached ({self.stats.consecutive_losses})",
                confidence=100,
                risk_level="high",
            )

        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _skip(self, reason: str, analysis: dict) -> AIDecision:
        self.stats.rounds_since_last_bet += 1
        return AIDecision(
            action="skip",
            reasoning=reason,
            confidence=0,
            risk_level="low",
            analysis=analysis if isinstance(analysis, dict) else {},
        )

# ---------------------------------------------------------------------------
# Backtesting & Dynamic Optimization
# ---------------------------------------------------------------------------

def backtest_simulation(config: AIConfig, multipliers: List[float], shared_cache: Optional[dict] = None) -> dict:
    """
    Simulates AIStrategyEngine over historical multipliers.
    multipliers: List of float, oldest round first (index 0 is oldest, index -1 is newest).
    """
    engine = AIStrategyEngine(db_session_factory=None, config=config, shared_cache=shared_cache)
    engine.stats = SessionStats(
        session_id="backtest",
        started_at=datetime.utcnow().isoformat(),
        current_balance=config.bankroll,
        starting_balance=config.bankroll,
        peak_balance=config.bankroll,
        lowest_balance=config.bankroll,
        is_running=True,
        is_dry_run=True,
    )
    engine.stats.equity_curve.append(config.bankroll)

    min_points = config.min_data_points
    if len(multipliers) < min_points:
        return {
            "profit_loss": 0.0,
            "roi": 0.0,
            "total_bets": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "drawdown": 0.0,
            "final_balance": config.bankroll,
            "ruined": False
        }

    # Step through the multipliers sequence
    for i in range(min_points, len(multipliers)):
        # Check session limits first
        limit_decision = engine._check_session_limits()
        if limit_decision and limit_decision.action == "stop_session":
            break

        # Cooldown check
        if engine.stats.consecutive_losses > 0 and engine.stats.rounds_since_last_bet < config.cooldown_after_loss:
            engine.stats.rounds_since_last_bet += 1
            continue

        # Extract history window up to current round (recent-first order expected by make_decision)
        history_window = list(reversed(multipliers[i - min_points:i]))
        actual_val = multipliers[i]

        decision = engine.make_decision(history_window)
        if decision.action == "bet":
            engine.record_outcome(actual_val, decision)
        elif decision.action == "skip":
            engine.record_outcome(actual_val, decision)

        # Ruin condition
        if engine.stats.current_balance < 10.0:
            break

    stats = engine.stats
    max_drawdown = stats.peak_balance - stats.lowest_balance

    return {
        "profit_loss": round(stats.total_profit_loss, 2),
        "roi": round(stats.roi, 1),
        "total_bets": stats.total_bets,
        "wins": stats.wins,
        "losses": stats.losses,
        "win_rate": stats.win_rate,
        "drawdown": round(max_drawdown, 2),
        "final_balance": round(stats.current_balance, 2),
        "ruined": stats.current_balance < 10.0
    }


def optimize_parameters(multipliers: List[float], base_config: AIConfig) -> dict:
    """
    Finds the optimal configuration parameters for AIStrategyEngine by using
    a Genetic Algorithm (Evolutionary Search) to optimize weights and thresholds.
    """
    import random
    shared_cache = {}

    def clamp(val, min_val, max_val):
        return max(min_val, min(max_val, val))

    def mutate_param(val, bounds, step, is_int=False):
        if random.random() > 0.3:  # 30% chance to mutate
            return val
        if is_int:
            drift = random.choice([-step, 0, step])
            return clamp(int(val + drift), bounds[0], bounds[1])
        else:
            drift = random.uniform(-step, step)
            return clamp(val + drift, bounds[0], bounds[1])

    def mutate(cfg: AIConfig) -> AIConfig:
        new_cfg = AIConfig(
            bankroll=cfg.bankroll,
            risk_level=cfg.risk_level,
            max_bet_pct=cfg.max_bet_pct,
            max_bet_abs=cfg.max_bet_abs,
            stop_loss_pct=cfg.stop_loss_pct,
            take_profit_pct=cfg.take_profit_pct,
            dry_run=cfg.dry_run,
            preferred_targets=cfg.preferred_targets,
            kelly_fraction=mutate_param(cfg.kelly_fraction, (0.05, 0.45), 0.05),
            confidence_threshold=mutate_param(cfg.confidence_threshold, (35.0, 70.0), 5.0),
            cooldown_after_loss=mutate_param(cfg.cooldown_after_loss, (1, 4), 1, is_int=True),
            analysis_window=mutate_param(cfg.analysis_window, (40, 120), 10, is_int=True),
            min_data_points=cfg.min_data_points,
            max_consecutive_losses=cfg.max_consecutive_losses,
            w_prob_high=mutate_param(cfg.w_prob_high, (0.0, 30.0), 3.0),
            w_prob_med=mutate_param(cfg.w_prob_med, (0.0, 20.0), 2.0),
            w_mr_oversold=mutate_param(cfg.w_mr_oversold, (0.0, 30.0), 3.0),
            w_vol_low=mutate_param(cfg.w_vol_low, (0.0, 20.0), 2.0),
            w_streak_low=mutate_param(cfg.w_streak_low, (0.0, 20.0), 2.0),
            w_data_quality=mutate_param(cfg.w_data_quality, (0.0, 15.0), 1.5),
            w_mr_overbought=mutate_param(cfg.w_mr_overbought, (0.0, 30.0), 3.0),
            w_vol_high=mutate_param(cfg.w_vol_high, (0.0, 30.0), 3.0),
            w_streak_high=mutate_param(cfg.w_streak_high, (0.0, 20.0), 2.0),
            w_prob_low=mutate_param(cfg.w_prob_low, (0.0, 30.0), 3.0),
            w_loss_penalty=mutate_param(cfg.w_loss_penalty, (0.0, 20.0), 2.0),
            weight_ev=mutate_param(cfg.weight_ev, (0.1, 0.9), 0.1),
            weight_prob=0.0
        )
        new_cfg.weight_prob = round(1.0 - new_cfg.weight_ev, 2)
        new_cfg.min_data_points = min(cfg.min_data_points, new_cfg.analysis_window)
        return new_cfg

    def crossover(parent1: AIConfig, parent2: AIConfig) -> AIConfig:
        child_params = {}
        for field in parent1.__dataclass_fields__:
            if field in ['bankroll', 'risk_level', 'max_bet_pct', 'max_bet_abs', 'stop_loss_pct', 'take_profit_pct', 'dry_run', 'preferred_targets', 'min_data_points', 'max_consecutive_losses']:
                child_params[field] = getattr(parent1, field)
            elif field == 'weight_prob':
                continue
            else:
                child_params[field] = getattr(parent1 if random.random() > 0.5 else parent2, field)
        child = AIConfig(**child_params)
        child.weight_prob = round(1.0 - child.weight_ev, 2)
        child.min_data_points = min(parent1.min_data_points, child.analysis_window)
        return child

    # Evaluate baseline
    baseline_stats = backtest_simulation(base_config, multipliers, shared_cache)

    # Genetic Algorithm Parameters
    pop_size = 30
    generations = 15
    elite_size = 6

    # Seed population
    population = [base_config]
    for _ in range(pop_size - 1):
        population.append(mutate(base_config))

    best_overall_config = base_config
    best_overall_stats = baseline_stats
    best_overall_score = baseline_stats["profit_loss"] - (0.5 * baseline_stats["drawdown"])
    if baseline_stats["ruined"]:
        best_overall_score = -99999.0

    for gen in range(generations):
        evaluated_pop = []
        for ind in population:
            stats = backtest_simulation(ind, multipliers, shared_cache)
            score = stats["profit_loss"] - (0.5 * stats["drawdown"])
            if stats["ruined"]:
                score = -99999.0
            if stats["total_bets"] < 5:
                score -= 50.0
            evaluated_pop.append((ind, stats, score))

        # Sort
        evaluated_pop.sort(key=lambda x: x[2], reverse=True)

        # Update best overall
        current_best_ind, current_best_stats, current_best_score = evaluated_pop[0]
        if current_best_score > best_overall_score:
            best_overall_score = current_best_score
            best_overall_config = current_best_ind
            best_overall_stats = current_best_stats

        # Keep elites
        new_population = []
        for i in range(elite_size):
            new_population.append(evaluated_pop[i][0])

        # Fill rest of population
        while len(new_population) < pop_size:
            tournament = random.sample(evaluated_pop[:15], 3)
            tournament.sort(key=lambda x: x[2], reverse=True)
            parent1 = tournament[0][0]

            tournament2 = random.sample(evaluated_pop[:15], 3)
            tournament2.sort(key=lambda x: x[2], reverse=True)
            parent2 = tournament2[0][0]

            child = crossover(parent1, parent2)
            child = mutate(child)
            new_population.append(child)

        population = new_population

    improvement_pct = 0.0
    if abs(baseline_stats["profit_loss"]) > 0:
        improvement_pct = round(((best_overall_stats["profit_loss"] - baseline_stats["profit_loss"]) / abs(baseline_stats["profit_loss"])) * 100, 1)
    else:
        improvement_pct = round(best_overall_stats["profit_loss"] * 100, 1)

    return {
        "original_stats": baseline_stats,
        "optimized_stats": best_overall_stats,
        "optimized_config": best_overall_config,
        "improvement_pct": improvement_pct
    }
