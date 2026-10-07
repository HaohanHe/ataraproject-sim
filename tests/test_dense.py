"""Unit tests for agent_core.dense (mechanism S2: dense patch fields + refinement).

Run from the project root:

    python3 tests/test_dense.py

Uses only unittest + the standard library; no runner, no SurveyState, no truth
files.  Synthetic targets: three tight ~2.5 deg clusters (one straddling the
RA=0/360 seam) plus uniform scatter.
"""
from __future__ import annotations

import math
import os
import sys
import unittest
from pathlib import Path

# Make the project root importable when this file is launched directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.dense import (  # noqa: E402
    DENSE_BIN_DEG,
    DENSE_FIBERS,
    N_DENSE,
    REFINE_ROUNDS,
    REFINE_STEP_DEG,
    DensePlanner,
)
from agent_core.geometry import radec_to_altaz, shift_altaz, tangent_offsets  # noqa: E402


class FakeGrid:
    """Minimal 4x4 fibre grid exposing only what DensePlanner uses."""

    side = 4
    n = 16
    pitch = 1.6
    fov = 6.4
    glass = 1.4

    def fiber_center(self, fiber):
        row, col = divmod(fiber, self.side)
        middle = (self.side - 1) / 2.0
        return (row - middle) * self.pitch, (col - middle) * self.pitch


def _build_catalogue():
    """Three tight clusters + uniform scatter.  Returns (ra, dec, cluster_centers)."""
    rng = __import__("random").Random(42)
    ra, dec = [], []
    clusters = [(100.0, 20.0, 26), (200.0, 40.0, 26), (359.7, 1.5, 20)]
    for cra, cdec, count in clusters:
        for _ in range(count):
            ra.append((cra + rng.uniform(-0.5, 0.5)) % 360.0)
            dec.append(cdec + rng.uniform(-0.5, 0.5))
    for _ in range(60):  # scatter
        ra.append(rng.uniform(0.0, 360.0))
        dec.append(rng.uniform(-60.0, 60.0))
    return ra, dec, clusters


def _angular_close(ra1, dec1, ra2, dec2, tol):
    """Small-angle sep (deg) -- clusters are far from the pole, fine for the test."""
    dra = (ra1 - ra2 + 180.0) % 360.0 - 180.0
    ddec = dec1 - dec2
    return math.hypot(dra, ddec) <= tol


class DenseBinTest(unittest.TestCase):
    def setUp(self):
        self.ra, self.dec, self.clusters = _build_catalogue()
        self.indices = list(range(len(self.ra)))
        self.planner = DensePlanner(FakeGrid())

    def test_top_bins_are_clusters(self):
        centers = self.planner.bin_centers(self.indices, self.ra, self.dec)
        # bounded by N_DENSE and non-empty
        self.assertGreater(len(centers), 0)
        self.assertLessEqual(len(centers), N_DENSE)
        # every cluster centroid must be represented by a returned centre
        for cra, cdec, _count in self.clusters:
            hit = any(_angular_close(cr, cd, cra, cdec, 2.0)
                      for cr, cd, _n in centers)
            self.assertTrue(hit, f"cluster near ({cra},{cdec}) missing")
        # the densest bins are clearly denser than the (count-1) scatter bins
        self.assertGreaterEqual(centers[0][2], 10)
        # bin size is the configured 2.5 deg
        self.assertAlmostEqual(DENSE_BIN_DEG, 2.5)

    def test_seam_cluster_not_split(self):
        """The cluster straddling ra=0/360 must surface as one centroid near 358/0."""
        centers = self.planner.bin_centers(self.indices, self.ra, self.dec)
        near_seam = [c for c in centers
                     if c[1] < 3.0 and (c[0] > 355.0 or c[0] < 5.0)]
        # exactly one dense bin (the merged seam cluster) sits near the seam;
        # scatter bins near there are singletons
        dense = [c for c in near_seam if c[2] >= 12]
        self.assertEqual(len(dense), 1)
        self.assertGreaterEqual(dense[0][2], 15)

    def test_candidate_pointings_in_bounds(self):
        centers = self.planner.bin_centers(self.indices, self.ra, self.dec)
        lst, lat, min_alt = 100.0, 30.0, 30.0
        pts = self.planner.candidate_pointings(centers, lst, lat, min_alt)
        self.assertGreater(len(pts), 0)
        for ra_c, dec_c, c_alt, c_az, fiber in pts:
            self.assertIn(fiber, DENSE_FIBERS)
            self.assertGreaterEqual(c_alt, min_alt + 1.5)
            self.assertLessEqual(c_alt, 89.0)
            self.assertGreaterEqual(c_az, 0.0)
            self.assertLess(c_az, 360.0)
            # contract: the centroid must land on the chosen fibre, i.e. its
            # gnomonic offset from the field centre equals the fibre offset
            a_alt, a_az = radec_to_altaz(ra_c, dec_c, lst, lat)
            off = tangent_offsets(a_alt, a_az, c_alt, c_az)
            self.assertIsNotNone(off)
            want = FakeGrid().fiber_center(fiber)
            self.assertAlmostEqual(off[0], want[0], delta=0.2)
            self.assertAlmostEqual(off[1], want[1], delta=0.2)


class RefineTest(unittest.TestCase):
    def setUp(self):
        self.start = (60.0, 120.0)
        # optimum 0.2 deg east of the start, at the same altitude
        self.opt = shift_altaz(*self.start, 0.0, 0.2)

    def _maker_score(self):
        calls = []

        def score(alt, az):
            calls.append((alt, az))
            # negative distance (deg) to the optimum
            dn, de = tangent_offsets(alt, az, self.opt[0], self.opt[1])
            return -math.hypot(dn or 0.0, de or 0.0)
        return score, calls

    def test_finds_better_neighbour(self):
        planner = DensePlanner(FakeGrid())
        score, calls = self._maker_score()
        b_alt, b_az, b_score = planner.refine(
            self.start[0], self.start[1], score, alt_lo=20.0)
        start_score = self._maker_score()[0](*self.start)
        self.assertGreaterEqual(b_score, start_score)
        # moved toward the east-shifted optimum
        self.assertGreater(len(calls), 1)
        dn, de = tangent_offsets(b_alt, b_az, self.start[0], self.start[1])
        self.assertGreater(de, REFINE_STEP_DEG * 0.5)
        # stayed inside the allowed window
        self.assertGreaterEqual(b_alt, 20.0)
        self.assertLessEqual(b_alt, 89.0)

    def test_no_worse_when_already_optimal(self):
        planner = DensePlanner(FakeGrid())
        score, calls = self._maker_score()
        o_alt, o_az, o_score = planner.refine(
            self.opt[0], self.opt[1], score, alt_lo=20.0)
        self.assertAlmostEqual(o_score, 0.0, places=3)
        self.assertLessEqual(len(calls), 1 + REFINE_ROUNDS * 8)

    def test_respects_altitude_floor(self):
        # optimum is far below the floor: refinement must never leave the window
        bad_opt = (5.0, 120.0)
        planner = DensePlanner(FakeGrid())

        def score(alt, az):
            return -abs(alt - bad_opt[0])
        b_alt, _b_az, _s = planner.refine(70.0, 120.0, score, alt_lo=25.0)
        self.assertGreaterEqual(b_alt, 25.0)
        self.assertLessEqual(b_alt, 89.0)


class DisabledSwitchTest(unittest.TestCase):
    """OBS_ENABLE_DENSE=0 must short-circuit both features."""

    def test_off(self):
        os.environ["OBS_ENABLE_DENSE"] = "0"
        try:
            planner = DensePlanner(FakeGrid())
            self.assertFalse(planner.enabled)
            ra, dec, _ = _build_catalogue()
            self.assertEqual(planner.bin_centers(range(len(ra)), ra, dec), [])
            self.assertEqual(
                planner.candidate_pointings([(100.0, 20.0, 20)], 100.0, 30.0, 30.0),
                [])

            def score(_alt, _az):
                return 1.0
            alt, az, value = planner.refine(60.0, 120.0, score)
            self.assertAlmostEqual(alt, 60.0)
            self.assertAlmostEqual(az, 120.0)
            self.assertAlmostEqual(value, 1.0)
        finally:
            del os.environ["OBS_ENABLE_DENSE"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
