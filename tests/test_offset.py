#!/usr/bin/env python3
"""Unit tests for agent_core.offset.OffsetLearner.

Run from the project root:
    python3 tests/test_offset.py

Uses synthetic hit/miss records only -- no runner, no truth files, deterministic.
The file self-bootstraps sys.path so it runs with plain unittest (no pytest).
"""
from __future__ import annotations

import math
import os
import random
import sys
import unittest

# Make the project root importable when invoked as `python3 tests/test_offset.py`.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from agent_core.geometry import FiberGrid  # noqa: E402
from agent_core.offset import OffsetLearner  # noqa: E402


def l1_like_grid() -> FiberGrid:
    """A 4x4 (16-fibre) grid like card L1: pitch == glass (gap 0), FOV ~2.53 deg."""
    fiber_side = math.sqrt(0.4)  # 0.63246 deg
    return FiberGrid({
        "grid_side": 4,
        "n_fibers": 16,
        "glass_side_deg": fiber_side,
        "pitch_deg": fiber_side,
        "fov_side_deg": 4.0 * fiber_side,
    })


def simulate_records(grid: FiberGrid, true_d_alt: float, true_d_az: float,
                     n: int = 320, seed: int = 20261007):
    """Given a *true* hard-card bias (true_d_alt, true_d_az) in degrees, scatter `n`
    targets uniformly across the field and build the records the learner would see:
    the fibre we assigned each target to, and whether the engine (which applies the
    hidden bias) reports a hit.

    Real targets fall at arbitrary sky positions, so we use a seeded uniform scatter
    (not a regular grid): a small fraction of targets happen to stay on their assigned
    fibre after the shift -- those hits are the anchor that lets the search pin the
    bias, while the misses carry the rest of the signal.
    """
    rng = random.Random(seed)
    recs = []
    half = grid.fov / 2.0 - 0.05
    tries = 0
    while len(recs) < n and tries < n * 40:
        tries += 1
        x = rng.uniform(-half, half)
        y = rng.uniform(-half, half)
        fiber, _margin = grid.classify(x, y)
        if fiber is None:
            continue
        # Engine sees the target shifted by -true_bias relative to the command centre.
        eng_fiber, eng_margin = grid.classify(x - true_d_alt, y - true_d_az)
        hit = (eng_fiber == fiber) and (eng_margin >= 0.0)
        recs.append((x, y, fiber, hit))
    return recs


class TestOffsetLearner(unittest.TestCase):
    def setUp(self):
        self.grid = l1_like_grid()

    # ------------------------------------------------------------------ gates
    def test_not_converged_before_enough_evidence(self):
        learner = OffsetLearner(self.grid)
        # Only a handful of records, almost no misses -> must stay unconverged.
        recs = simulate_records(self.grid, 0.55, 0.42, n=8)
        learner.add_observation(recs)
        self.assertFalse(learner.converged)
        self.assertIsNone(learner.learned)
        # compensate must be the identity before convergence.
        alt, az = learner.compensate(60.0, 120.0)
        self.assertAlmostEqual(alt, 60.0, places=6)
        self.assertAlmostEqual(az, 120.0, places=6)

    # ------------------------------------------------------------------ margin gate
    def test_margin_gate_blocks_adoption_before_enough_evidence(self):
        # >=6 misses triggers the search, but with only a handful of records the best
        # candidate cannot beat the runner-up by OFFSET_MARGIN=8 results, so it must NOT
        # be adopted yet.
        true_alt, true_az = 0.55, 0.42
        learner = OffsetLearner(self.grid)
        few = simulate_records(self.grid, true_alt, true_az, n=14, seed=7)
        misses = sum(1 for r in few if not r[3])
        self.assertGreaterEqual(misses, 6, "fixture needs >=6 misses to trigger search")
        learner.add_observation(few)
        self.assertFalse(learner.converged, "margin < 8 must block adoption")
        self.assertIsNone(learner.learned)
        alt, az = learner.compensate(60.0, 120.0)
        self.assertAlmostEqual(alt, 60.0, places=6)

    # --------------------------------------------------------------- learning
    def test_converges_near_true_offset_and_compensates(self):
        # A known hard-card bias (~0.4-0.7 deg, both axes non-zero).
        true_alt, true_az = 0.42, 0.33
        learner = OffsetLearner(self.grid)
        recs = simulate_records(self.grid, true_alt, true_az, n=1000, seed=1)
        learner.add_observation(recs)
        self.assertTrue(learner.converged, "should converge after enough evidence")

        self.assertIsNotNone(learner.learned)
        learned_alt, learned_az = learner.learned
        # Coarse lattice is pitch/12 ~= 0.0527 deg; tolerance ~one coarse step.
        self.assertAlmostEqual(learned_alt, true_alt, delta=0.06)
        self.assertAlmostEqual(learned_az, true_az, delta=0.06)

        # compensate must apply the *reverse* sign: command = desired - learned.
        desired_alt, desired_az = 63.0, 150.0
        out_alt, out_az = learner.compensate(desired_alt, desired_az)
        self.assertAlmostEqual(out_alt, desired_alt - learned_alt, delta=1e-6)
        self.assertAlmostEqual(out_az, (desired_az - learned_az) % 360.0, delta=1e-6)

    def test_compensate_direction_is_reverse_of_bias(self):
        # Direct check of the sign convention with a hand-built converged learner.
        learner = OffsetLearner(self.grid)
        learner.enabled = True
        learner.converged = True
        learner.learned = (0.30, -0.25)
        alt, az = learner.compensate(50.0, 10.0)
        self.assertAlmostEqual(alt, 49.70, places=6)       # 50 - 0.30
        self.assertAlmostEqual(az, (10.0 - (-0.25)) % 360.0, places=6)  # = 10.25

    # ------------------------------------------------------------------ edge
    def test_best_on_edge_triggers_expansion(self):
        # Shrink the initial search window so the true bias sits on its edge; the
        # learner must auto-expand half_steps and still converge.
        true_alt, true_az = 0.42, 0.33
        learner = OffsetLearner(self.grid)
        learner._half_steps = 6  # window +/-0.316 deg, just inside the true 0.42 bias
        recs = simulate_records(self.grid, true_alt, true_az, n=1000, seed=1)
        learner.add_observation(recs)
        self.assertGreater(learner._half_steps, 6, "window should have auto-expanded")
        self.assertTrue(learner.converged, "expansion should let it converge")
        learned_alt, learned_az = learner.learned
        self.assertAlmostEqual(learned_alt, true_alt, delta=0.06)
        self.assertAlmostEqual(learned_az, true_az, delta=0.06)

    # ------------------------------------------------------------------ switch
    def test_disabled_environment_switch_is_noop(self):
        os.environ["OBS_ENABLE_OFFSET"] = "0"
        try:
            learner = OffsetLearner(self.grid)
            recs = simulate_records(self.grid, 0.42, 0.33, n=200, seed=3)
            learner.add_observation(recs)
            self.assertFalse(learner.enabled)
            self.assertFalse(learner.converged)
            self.assertEqual(len(learner._records), 0, "disabled: nothing recorded")
            alt, az = learner.compensate(60.0, 120.0)
            self.assertAlmostEqual(alt, 60.0, places=6)
            self.assertAlmostEqual(az, 120.0, places=6)
        finally:
            os.environ["OBS_ENABLE_OFFSET"] = "1"


if __name__ == "__main__":
    unittest.main(verbosity=2)
