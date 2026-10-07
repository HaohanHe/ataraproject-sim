#!/usr/bin/env python3
"""Unit tests for agent_core.bandfit.BandFitter (constraint-satisfaction design).

Run from the project root:
    python3 tests/test_bandfit.py
"""
from __future__ import annotations

import os
import sys
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from agent_core.scoring import ScoringModel  # noqa: E402
from agent_core import bandfit as bf_mod  # noqa: E402
from agent_core.bandfit import BandFitter  # noqa: E402


def _make_scoring() -> ScoringModel:
    return ScoringModel(
        scoring_config={},
        site={"latitude_deg": 30.0, "longitude_deg": 0.0},
    )


def _feed_saturated(bf: BandFitter, hours: float, *, program: str, model: float,
                    weight: float = 100.0, flux: float = 1.0,
                    duration: float = 900.0, clean: bool = True) -> None:
    """Inject a saturated hit: score = weight * multiplier[program]."""
    mult = bf.scoring.program_multipliers[program]
    bf.on_result(
        hours,
        score=weight * mult, weight=weight, flux=flux, duration=duration,
        model=model, declared_program=program, clean=clean,
    )


class TestBandFitSaturated(unittest.TestCase):
    """Constraint-satisfaction: saturated hits bound band_sky."""

    def setUp(self):
        self.scoring = _make_scoring()
        self.bf = BandFitter(self.scoring, env={"OBS_ENABLE_BANDFIT": "1"})

    def test_dark_hits_give_lower_bound(self):
        """Two DARK hits with model=0.8 -> L = 0.65/0.8 = 0.8125."""
        _feed_saturated(self.bf, 1.0, program="DARK", model=0.8)
        _feed_saturated(self.bf, 1.1, program="DARK", model=0.8)
        level = self.bf.band_scale(hours=1.2)
        self.assertIsNotNone(level)
        self.assertAlmostEqual(level, 0.65 / 0.8, places=4)
        self.assertEqual(self.scoring.program_band(0.8 * level), "DARK")

    def test_varying_models_max_lower_bound(self):
        """A DARK hit at model=0.8 demands L=0.8125; one at model=1.0 demands L=0.65.
        The MAX (0.8125) is used."""
        _feed_saturated(self.bf, 1.0, program="DARK", model=1.0)
        _feed_saturated(self.bf, 1.1, program="DARK", model=0.8)
        level = self.bf.band_scale(hours=1.2)
        self.assertIsNotNone(level)
        self.assertAlmostEqual(level, 0.65 / 0.8, places=4)

    def test_bright_hits_between_bounds(self):
        """Two BRIGHT hits at model=1.0: L=0.40, U=0.65. Return L=0.40."""
        _feed_saturated(self.bf, 1.0, program="BRIGHT", model=1.0)
        _feed_saturated(self.bf, 1.1, program="BRIGHT", model=1.0)
        level = self.bf.band_scale(hours=1.2)
        self.assertIsNotNone(level)
        self.assertAlmostEqual(level, 0.40, places=4)
        self.assertEqual(self.scoring.program_band(1.0 * level), "BRIGHT")

    def test_backup_only_hits_return_half_upper(self):
        """Two BACKUP hits at model=1.0: L=0, U=0.40. Return 0.40*0.5=0.20."""
        _feed_saturated(self.bf, 1.0, program="BACKUP", model=1.0)
        _feed_saturated(self.bf, 1.1, program="BACKUP", model=1.0)
        level = self.bf.band_scale(hours=1.2)
        self.assertIsNotNone(level)
        self.assertAlmostEqual(level, 0.40 * 0.5, places=4)
        self.assertEqual(self.scoring.program_band(1.0 * level), "BACKUP")

    def test_dark_and_bright_consistent(self):
        """DARK (model=1.0): L=0.65. BRIGHT (model=0.8): L=0.5, U=0.8125.
        Combined L=0.65, U=0.8125. Feasible -> return 0.65."""
        _feed_saturated(self.bf, 1.0, program="DARK", model=1.0)
        _feed_saturated(self.bf, 1.1, program="BRIGHT", model=0.8)
        level = self.bf.band_scale(hours=1.2)
        self.assertIsNotNone(level)
        self.assertAlmostEqual(level, 0.65, places=4)

    def test_dark_and_backup_infeasible(self):
        """DARK (model=1.0): L=0.65. BACKUP (model=1.0): U=0.40.
        L=0.65 >= U=0.40 -> infeasible -> None."""
        _feed_saturated(self.bf, 1.0, program="DARK", model=1.0)
        _feed_saturated(self.bf, 1.1, program="BACKUP", model=1.0)
        self.assertIsNone(self.bf.band_scale(hours=1.2))

    def test_band_opt_scales_level(self):
        orig = bf_mod.BAND_OPT
        try:
            bf_mod.BAND_OPT = 1.1
            _feed_saturated(self.bf, 1.0, program="DARK", model=1.0)
            _feed_saturated(self.bf, 1.1, program="DARK", model=1.0)
            level = self.bf.band_scale(hours=1.2)
            self.assertIsNotNone(level)
            self.assertAlmostEqual(level, 0.65 * 1.1, places=4)
        finally:
            bf_mod.BAND_OPT = orig

    def test_old_samples_pruned(self):
        _feed_saturated(self.bf, 0.0, program="DARK", model=1.0)
        _feed_saturated(self.bf, 0.1, program="DARK", model=1.0)
        self.assertIsNone(self.bf.band_scale(hours=3.0))


class TestBandFitNonSaturated(unittest.TestCase):
    def setUp(self):
        self.scoring = _make_scoring()
        self.bf = BandFitter(self.scoring, env={"OBS_ENABLE_BANDFIT": "1"})

    def test_unsaturated_not_recorded(self):
        weight = 100.0
        mult = self.scoring.program_multipliers["DARK"]
        score = weight * 0.8 * mult
        self.bf.on_result(
            hours=1.0, score=score, weight=weight, flux=1.0, duration=900.0,
            model=1.0, declared_program="DARK", clean=True,
        )
        self.assertIsNone(self.bf.band_scale(hours=1.0))

    def test_mismatch_not_recorded(self):
        weight = 100.0
        score = weight * 1.0 * 1.0
        self.bf.on_result(
            hours=1.0, score=score, weight=weight, flux=1.0, duration=900.0,
            model=1.0, declared_program="DARK", clean=True,
        )
        self.assertIsNone(self.bf.band_scale(hours=1.0))

    def test_dirty_hit_excluded(self):
        weight = 100.0
        mult = self.scoring.program_multipliers["DARK"]
        self.bf.on_result(
            hours=1.0, score=weight * mult, weight=weight, flux=1.0, duration=900.0,
            model=1.0, declared_program="DARK", clean=False,
        )
        self.assertIsNone(self.bf.band_scale(hours=1.0))

    def test_single_sample_insufficient(self):
        _feed_saturated(self.bf, 1.0, program="DARK", model=1.0)
        self.assertIsNone(self.bf.band_scale(hours=1.0))


class TestBandFitEdgeCases(unittest.TestCase):
    def test_disabled_returns_none(self):
        bf = BandFitter(_make_scoring(), env={"OBS_ENABLE_BANDFIT": "0"})
        weight = 100.0
        mult = bf.scoring.program_multipliers["DARK"]
        bf.on_result(
            hours=1.0, score=weight * mult, weight=weight, flux=1.0, duration=900.0,
            model=1.0, declared_program="DARK", clean=True,
        )
        self.assertIsNone(bf.band_scale(hours=1.0))

    def test_no_samples_returns_none(self):
        bf = BandFitter(_make_scoring(), env={"OBS_ENABLE_BANDFIT": "1"})
        self.assertIsNone(bf.band_scale(hours=5.0))


class TestBandFitEfficiency(unittest.TestCase):
    def test_e_from_unsaturated_ratios(self):
        scoring = _make_scoring()
        bf = BandFitter(scoring, env={"OBS_ENABLE_BANDFIT": "1"})
        weight = 100.0
        mult_dark = scoring.program_multipliers["DARK"]
        _feed_saturated(bf, 1.0, program="DARK", model=1.0, weight=weight)
        _feed_saturated(bf, 1.1, program="DARK", model=1.0, weight=weight)
        self.assertAlmostEqual(bf.band_scale(1.2), 0.65, places=4)
        score = weight * 0.8 * mult_dark
        bf.on_result(
            hours=1.2, score=score, weight=weight, flux=1.0, duration=900.0,
            model=1.0, declared_program="DARK", clean=True,
        )
        bf.on_result(
            hours=1.3, score=score, weight=weight, flux=1.0, duration=900.0,
            model=1.0, declared_program="DARK", clean=True,
        )
        E = bf.instrument_efficiency(hours=1.4)
        self.assertIsNotNone(E)
        expected_ratio = 0.8 * scoring.f0t0 / (1.0 * 900.0 * 1.0)
        self.assertAlmostEqual(E, expected_ratio / 0.65, places=3)


if __name__ == "__main__":
    unittest.main()
