"""Unit tests for agent_core/lambda_pricing.py (Mechanism S3: time pricing).

Run from the project root:
    python3 tests/test_lambda_pricing.py

Stdlib unittest only; synthetic, deterministic data; no runner started.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta

# Make the project root importable regardless of the caller's cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent_core.lambda_pricing import (  # noqa: E402
    LAMBDA_EMA,
    LAMBDA_FRAC,
    SCARCITY_POWER,
    SCARCITY_REF,
    TimePricing,
)


def _make_calendar(n_nights: int = 10, night_hours: float = 8.0,
                   start: datetime = datetime(2026, 3, 1, 20, 0, 0)):
    """Ten synthetic nights, each `night_hours` long, starting at 20:00 local,
    spaced 24 h apart.  Total observable seconds = n_nights * night_hours * 3600.
    """
    nights = []
    for k in range(n_nights):
        s = start + timedelta(days=k)
        e = s + timedelta(hours=night_hours)
        nights.append((s, e))
    survey_start = nights[0][0]
    survey_end = nights[-1][1]
    return nights, survey_start, survey_end


class EMAGainRateTests(unittest.TestCase):
    """The EMA gain rate converges to a steady observed gain/second."""

    def test_converges_to_steady_rate(self):
        nights, ss, se = _make_calendar()
        tp = TimePricing(nights, ss, se, env={"OBS_ENABLE_LAMBDA": "1"})
        # Steady observation: gain 9 per 900 s exposure -> rate 0.01 /s.
        for _ in range(400):
            tp.update(gain=9.0, duration_seconds=900)
        # EMA: ema_N = r * (1 - (1-alpha)^N).  After 400 updates alpha=0.03,
        # this is ~0.01 * (1 - 0.97^400) ~= 0.01 (well above 99.5% of r).
        self.assertGreater(tp.ema_gain_rate, 0.01 * 0.99)
        self.assertLess(tp.ema_gain_rate, 0.0101)

    def test_starts_at_zero_and_rises_monotonically(self):
        nights, ss, se = _make_calendar()
        tp = TimePricing(nights, ss, se, env={"OBS_ENABLE_LAMBDA": "1"})
        self.assertEqual(tp.ema_gain_rate, 0.0)
        prev = 0.0
        for _ in range(50):
            tp.update(gain=9.0, duration_seconds=900)
            self.assertGreaterEqual(tp.ema_gain_rate, prev - 1e-12)
            prev = tp.ema_gain_rate

    def test_zero_or_negative_gain_depresses_rate(self):
        nights, ss, se = _make_calendar()
        tp = TimePricing(nights, ss, se, env={"OBS_ENABLE_LAMBDA": "1"})
        for _ in range(100):
            tp.update(gain=9.0, duration_seconds=900)
        warm = tp.ema_gain_rate
        for _ in range(50):
            tp.update(gain=0.0, duration_seconds=900)
        self.assertLess(tp.ema_gain_rate, warm)

    def test_ignores_nonpositive_duration(self):
        nights, ss, se = _make_calendar()
        tp = TimePricing(nights, ss, se, env={"OBS_ENABLE_LAMBDA": "1"})
        tp.update(gain=9.0, duration_seconds=0)
        tp.update(gain=9.0, duration_seconds=-5)
        self.assertEqual(tp.ema_gain_rate, 0.0)


class ScarcityTests(unittest.TestCase):
    def test_scarcity_zero_at_survey_start(self):
        nights, ss, se = _make_calendar()
        tp = TimePricing(nights, ss, se, env={"OBS_ENABLE_LAMBDA": "1"})
        self.assertAlmostEqual(tp.scarcity(ss), 0.0, places=6)

    def test_scarcity_one_after_last_night(self):
        nights, ss, se = _make_calendar()
        tp = TimePricing(nights, ss, se, env={"OBS_ENABLE_LAMBDA": "1"})
        self.assertAlmostEqual(tp.scarcity(se + timedelta(seconds=1)), 1.0, places=6)

    def test_scarcity_grows_monotonically(self):
        nights, ss, se = _make_calendar()
        tp = TimePricing(nights, ss, se, env={"OBS_ENABLE_LAMBDA": "1"})
        # Half-way through the season (5 nights of 10 used).
        halfway = ss + timedelta(days=5)
        s = tp.scarcity(halfway)
        self.assertGreater(s, 0.4)
        self.assertLess(s, 0.6)

    def test_scarcity_in_empty_calendar_is_zero(self):
        tp = TimePricing([], None, None, env={"OBS_ENABLE_LAMBDA": "1"})
        self.assertEqual(tp.scarcity(datetime(2026, 3, 1)), 0.0)


class LambdaScalingTests(unittest.TestCase):
    def _warmed(self, env=None):
        nights, ss, se = _make_calendar()
        tp = TimePricing(nights, ss, se, env=env or {"OBS_ENABLE_LAMBDA": "1"})
        for _ in range(400):
            tp.update(gain=9.0, duration_seconds=900)  # ema ~= 0.01
        return tp, ss, se

    def test_lambda_zero_when_scarcity_zero(self):
        tp, ss, se = self._warmed()
        self.assertEqual(tp.lambda_now(ss), 0.0)

    def test_lambda_caps_at_lambda_frac_times_ema_when_scarcity_at_ref(self):
        tp, ss, se = self._warmed()
        # After the last night scarcity == 1 >= SCARCITY_REF.
        lam = tp.lambda_now(se + timedelta(seconds=1))
        expected = LAMBDA_FRAC * tp.ema_gain_rate * (min(1.0, 1.0 / SCARCITY_REF) ** SCARCITY_POWER)
        self.assertAlmostEqual(lam, expected, places=6)
        self.assertGreater(lam, 0.0)

    def test_lambda_scales_linearly_with_scarcity(self):
        tp, ss, se = self._warmed()
        # Half-way: scarcity ~= 0.5, POWER=1, REF=1 -> scale 0.5.
        halfway = ss + timedelta(days=5)
        s = tp.scarcity(halfway)
        lam = tp.lambda_now(halfway)
        self.assertAlmostEqual(lam, LAMBDA_FRAC * tp.ema_gain_rate * s, places=6)
        # And it sits between the start (0) and the end (capped).
        self.assertGreater(lam, 0.0)
        self.assertLess(lam, LAMBDA_FRAC * tp.ema_gain_rate)

    def test_lambda_zero_before_first_update(self):
        nights, ss, se = _make_calendar()
        tp = TimePricing(nights, ss, se, env={"OBS_ENABLE_LAMBDA": "1"})
        # ema still 0 even deep into the season.
        self.assertEqual(tp.lambda_now(se), 0.0)


class SwitchOffTests(unittest.TestCase):
    def test_lambda_always_zero_when_off(self):
        nights, ss, se = _make_calendar()
        tp = TimePricing(nights, ss, se, env={"OBS_ENABLE_LAMBDA": "0"})
        for _ in range(400):
            tp.update(gain=9.0, duration_seconds=900)
        for now in (ss, ss + timedelta(days=5), se):
            self.assertEqual(tp.lambda_now(now), 0.0)

    def test_net_gain_equals_gain_when_off(self):
        nights, ss, se = _make_calendar()
        tp = TimePricing(nights, ss, se, env={"OBS_ENABLE_LAMBDA": "0"})
        for _ in range(400):
            tp.update(gain=9.0, duration_seconds=900)
        for T in (300, 900, 3600):
            self.assertEqual(tp.net_gain(123.4, T, se), 123.4)

    def test_default_switch_is_on(self):
        # No env passed -> uses os.environ; default "1".  If the test runner has
        # exported OBS_ENABLE_LAMBDA=0 we still assert only that the attribute
        # reads the documented default path.
        nights, ss, se = _make_calendar()
        old = os.environ.pop("OBS_ENABLE_LAMBDA", None)
        try:
            tp = TimePricing(nights, ss, se)
            self.assertTrue(tp.enabled)
        finally:
            if old is not None:
                os.environ["OBS_ENABLE_LAMBDA"] = old


class NetGainComparisonTests(unittest.TestCase):
    """A worked example: with a high late-season lambda, the longer exposure
    wins on raw gain but loses on net gain."""

    def test_long_exposure_penalised_late_season(self):
        nights, ss, se = _make_calendar()
        tp = TimePricing(nights, ss, se, env={"OBS_ENABLE_LAMBDA": "1"})
        for _ in range(400):
            tp.update(gain=9.0, duration_seconds=900)  # ema ~= 0.01 /s

        now = se  # late season: scarcity ~= 1, lambda ~= 0.6 * 0.01 = 0.006 /s
        # Two candidate exposures for the SAME pointing:
        #   short: T=900 s, raw gain 9.0   (rate 0.0100 /s)
        #   long : T=3600 s, raw gain 24.0 (rate 0.00667 /s; saturating)
        net_short = tp.net_gain(9.0, 900, now)
        net_long = tp.net_gain(24.0, 3600, now)
        # Raw gain says "long" is better (24 > 9).
        self.assertGreater(24.0, 9.0)
        # Net gain says "short" is better:
        self.assertGreater(net_short, net_long)
        # And the time price is non-trivial (~0.006 * 3600 = 21.6 points).
        self.assertLess(net_long, 24.0)

    def test_early_season_no_penalty_keeps_raw_ranking(self):
        nights, ss, se = _make_calendar()
        tp = TimePricing(nights, ss, se, env={"OBS_ENABLE_LAMBDA": "1"})
        for _ in range(400):
            tp.update(gain=9.0, duration_seconds=900)
        # At survey start scarcity=0 -> lambda=0 -> net == raw gain.
        self.assertEqual(tp.net_gain(9.0, 900, ss), 9.0)
        self.assertEqual(tp.net_gain(24.0, 3600, ss), 24.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
