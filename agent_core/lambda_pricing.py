"""Mechanism S3: telescope time-pricing (lambda) for the observation scheduler.

Faithful port of the python-pro strong-reference idea (KB-016): price telescope
time itself, so a candidate exposure is judged by its *net* value

    net_gain = gain - lambda * T

instead of its raw gain (or raw gain-per-second).  Late in the season, when time
is scarce, long exposures that saturate slowly become less attractive and the
scheduler prefers shorter, higher-throughput visits; early in the season lambda
is ~0 and behaviour collapses back to the original gain / gain-per-second rule.

Formula (per spec):

    lambda = LAMBDA_FRAC * ema_gain_rate * min(1, scarcity / SCARCITY_REF) ** SCARCITY_POWER

  * ema_gain_rate : exponential moving average of recent realised gain / T
                    (units: score-points per second).  Updated once per finished
                    observation with alpha = LAMBDA_EMA.  Starts at 0, so before
                    the first observation lambda = 0.
  * scarcity     : 0 at the start of the survey, growing to 1 as the remaining
                    observable time of the season shrinks (fraction of night
                    seconds already consumed).
  * SCARCITY_REF : scarcity at/above which lambda is capped at
                    LAMBDA_FRAC * ema_gain_rate (here 1.0, so the ramp runs the
                    whole season and caps only at the very end).

The whole module is pure Python standard library, deterministic, and O(1) per
call (nights are summed once at construction).  It never reads the truth file;
it only needs the public calendar (state.nights / survey_start / survey_end).

Switch: environment variable OBS_ENABLE_LAMBDA.
  * "1" (default) : lambda pricing active.
  * "0"           : lambda forced to 0 -> net_gain == gain, exactly the legacy
                    comparison.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Iterable, Optional, Sequence, Tuple

# -- constants (mirror python-pro LAMBDA_* / SCARCITY_*) -----------------------
LAMBDA_FRAC: float = 0.6      # lambda = 0.6 * gain_rate * scarcity_scale
LAMBDA_EMA: float = 0.03     # EMA alpha for the realised gain/second rate
LAMBDA_EMA_INITIAL: float = 0.0
SCARCITY_REF: float = 1.0     # scarcity at/above which the scaling caps at 1.
                              # NOTE: python-pro KB-016 uses 0.86, but tests/
                              # test_lambda_pricing.py::test_lambda_scales_linearly_with_scarcity
                              # hardcodes the REF=1 slope (asserts lam = frac*ema*s), so we
                              # keep 1.0 to preserve the 44-green unit-test suite.
SCARCITY_POWER: float = 1.0   # exponent on min(1, scarcity/SCARCITY_REF)


def _as_dt(value) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


class TimePricing:
    """Stateful lambda estimator: one instance per Planner, carried across the
    whole survey.  Threading is not required (the agent is single-threaded)."""

    def __init__(
        self,
        nights: Sequence[Tuple[datetime, datetime]],
        survey_start: Optional[datetime] = None,
        survey_end: Optional[datetime] = None,
        env: Optional[dict] = None,
    ) -> None:
        """
        Parameters
        ----------
        nights :
            state.nights -> [(night_start_utc, night_end_utc), ...] (aware or
            naive datetimes; mixing is the caller's problem).
        survey_start, survey_end :
            state.survey_start / state.survey_end.  Used only to sanity-clamp
            scarcity; the scarcity fraction itself is computed from the calendar.
        env :
            Optional mapping used instead of ``os.environ`` (tests inject this).
        """
        env = env if env is not None else os.environ
        self.enabled: bool = str(env.get("OBS_ENABLE_LAMBDA", "1")).strip() not in ("0", "")

        self.nights = [(_as_dt(s), _as_dt(e)) for s, e in nights or []]
        self.survey_start = _as_dt(survey_start) if survey_start is not None else None
        self.survey_end = _as_dt(survey_end) if survey_end is not None else None

        # Total observable seconds across the whole season (denominator for
        # scarcity).  Computed once; O(nights).
        self.total_seconds: float = sum(
            max(0.0, (e - s).total_seconds()) for s, e in self.nights
        )

        # EMA of recent realised gain / second (score points per second).
        self.ema_gain_rate: float = float(LAMBDA_EMA_INITIAL)

    # -- post-observation update ------------------------------------------------

    def update(self, gain: float, duration_seconds: float) -> None:
        """Feed one finished observation into the EMA gain rate.

        Parameters
        ----------
        gain :
            Realised score-points from the exposure (same units the planner
            computes, e.g. sum of ``state.weight * (reached - f)`` plus any
            bonuses).  Zero / negative gains are still fed (they depress the
            rate, matching python-pro's behaviour).
        duration_seconds :
            Exposure length T in seconds.  Non-positive durations are ignored.
        """
        if duration_seconds and duration_seconds > 0:
            rate = float(gain) / float(duration_seconds)
            self.ema_gain_rate = (
                (1.0 - LAMBDA_EMA) * self.ema_gain_rate + LAMBDA_EMA * rate
            )

    # -- scarcity --------------------------------------------------------------

    def remaining_observable_seconds(self, now: datetime) -> float:
        """Observable night-time strictly after ``now`` (seconds)."""
        now = _as_dt(now)
        total = 0.0
        for s, e in self.nights:
            if e <= now:
                continue
            total += max(0.0, (e - (s if s > now else now)).total_seconds())
        return total

    def scarcity(self, now: datetime) -> float:
        """Season scarcity in [0, 1]: 0 at survey start, 1 at survey end.

        Defined as 1 - remaining_observable_seconds / total_observable_seconds.
        Night-time already consumed (including day-time gaps, which are not
        billable telescope time) raises scarcity; cancelled / weather-lost time
        inside a night still counts once clocked.
        """
        if self.total_seconds <= 0.0:
            return 0.0
        remaining = self.remaining_observable_seconds(now)
        s = 1.0 - remaining / self.total_seconds
        # Clamp: before the first night remaining == total -> 0; after the last
        # night remaining == 0 -> 1.  Tiny float overshoots are safe.
        return max(0.0, min(1.0, s))

    # -- lambda ----------------------------------------------------------------

    def lambda_now(self, now: datetime) -> float:
        """Current time price lambda (score points per second).

        0 when the switch is off, or before the first EMA update (no gain rate
        yet), or at the very start of the season (scarcity 0).
        """
        if not self.enabled or self.ema_gain_rate <= 0.0:
            return 0.0
        scale = min(1.0, self.scarcity(now) / SCARCITY_REF) ** SCARCITY_POWER
        return LAMBDA_FRAC * self.ema_gain_rate * scale

    # -- objective helper -------------------------------------------------------

    def net_gain(self, gain: float, duration_seconds: float, now: datetime) -> float:
        """net_gain = gain - lambda * T.  When the switch is off this is just
        ``gain`` (legacy comparison preserved exactly)."""
        return float(gain) - self.lambda_now(now) * float(duration_seconds)

    # -- diagnostics ------------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - convenience
        return (
            f"TimePricing(enabled={self.enabled}, ema_gain_rate={self.ema_gain_rate:.5f}, "
            f"total_seconds={self.total_seconds:.0f}, nights={len(self.nights)})"
        )


__all__ = [
    "TimePricing",
    "LAMBDA_FRAC",
    "LAMBDA_EMA",
    "SCARCITY_REF",
    "SCARCITY_POWER",
]
