"""Hard-card fixed pointing-offset learner (mechanism S1).

A hard card carries one *unpublished, fixed* pointing bias: the telescope does not
actually point where we command -- the real center is the command plus a constant
``(d_alt, d_az)`` (see ``v4_fiber_map.FiberGrid.actual_center``). The agent can never
read that truth, but it can *infer* it from its own hit/miss history: for every
assigned target we know which fibre we expected it on and where it landed relative to
the commanded centre; the engine tells us (via ``last_result.hits``) whether that
target really landed on its assigned fibre's glass.

Given a candidate bias ``o = (o_alt, o_az)``, we predict the engine would have shifted
every target's tangent offset by ``-o`` (gnomonic projection is a local translation),
so a target assigned to fibre ``f`` at tangent offset ``(x, y)`` would hit iff
``classify(x - o_alt, y - o_az) == f`` (on glass). We search the bias grid for the
``o`` that best explains the observed hit/miss pattern, then *reverse-compensate*:
to make the real centre land where the planner intended, we command
``desired - o``.

Design notes
------------
* Pure standard library. No truth files are read.
* The steady-state decision path is O(records); the grid search runs at most once
  every ``OFFSET_REFINE_EVERY`` observations and is bounded, so it never blocks the
  ms-scale main loop for more than a few hundred milliseconds on a slow decision.
* Anti-overfitting gates: we only *adopt* a bias once we have seen
  ``OFFSET_MIN_MISSES`` assigned-but-missed targets AND the best candidate explains at
  least ``OFFSET_MARGIN`` more results than the runner-up. Until then ``compensate``
  returns the pointing unchanged, so the agent behaves exactly as before.
* ``OBS_ENABLE_OFFSET=0`` (or unset-off) makes this module a no-op: learning is
  skipped and ``compensate`` is the identity.

This is a *new, standalone* module. It intentionally does not edit planner.py /
state.py / geometry.py; the serial integration step wires it in (see the contract at
the bottom of this file).
"""
from __future__ import annotations

import math
import os
from collections import deque
from typing import Callable, Deque, Dict, Iterable, List, Optional, Tuple

from .geometry import FiberGrid, tangent_offsets

# --- tunables (mirror the verified pro-agent constants; KB-018) -----------------
OFFSET_STEPS = 16          # coarse grid half-width, in steps (~1/3 FOV total width)
OFFSET_MIN_MISSES = 6      # need this many assigned-but-missed before any estimate
OFFSET_MARGIN = 8          # best must beat runner-up by this many explained results
OFFSET_FINE_EVIDENCE = 150  # observations before we are allowed to refine
OFFSET_REFINE_EVERY = 10   # re-search / refine at most once per this many observations
_COARSE_DIV = 12.0         # coarse step = pitch / 12
_FINE_DIV = 4.0            # fine step = coarse step / 4
_FINE_WINDOW = 2          # fine search spans +/- this many coarse steps around best
_MAX_EXPANSIONS = 2       # auto-expand the coarse window at most this many times
_MAX_RECORDS = 800         # ring buffer cap (fixed bias; old records stay valid)

# A record is (x, y, assigned_fiber, hit):
#   x, y           tangent-plane offset of the target from the COMMANDED centre (deg)
#   assigned_fiber fibre id the planner put the target on (int)
#   hit            True iff the engine reported the target in last_result.hits
Record = Tuple[float, float, int, bool]


class OffsetLearner:
    """Infer the fixed hard-card pointing bias from hit/miss outcomes."""

    def __init__(self, grid: FiberGrid, log: Optional[Callable[[str], None]] = None):
        self.grid = grid
        self.log = log or (lambda text: None)
        self.pitch = float(grid.pitch)
        self.side = int(grid.side)
        self.fov = float(grid.fov)
        self.glass = float(grid.glass)

        self.enabled = os.environ.get("OBS_ENABLE_OFFSET", "0") != "0"

        self._records: Deque[Record] = deque(maxlen=_MAX_RECORDS)
        self._misses = 0          # assigned-but-missed targets seen
        self._obs = 0             # observations (records) ingested
        self._since_search = 0     # records since last coarse search
        self._since_refine = 0    # records since last fine refinement
        self._expansions = 0

        self.converged = False
        self.learned: Optional[Tuple[float, float]] = None  # (d_alt, d_az) deg
        self._half_steps = OFFSET_STEPS

    # ------------------------------------------------------------------ ingest
    def add_observation(self, records: Iterable[Record]) -> None:
        """Feed one exposure's worth of (x, y, assigned_fiber, hit) records.

        This is the pure learning core -- fully unit-testable with synthetic data.
        """
        if not self.enabled:
            return
        added = 0
        for x, y, fiber, hit in records:
            self._records.append((float(x), float(y), int(fiber), bool(hit)))
            added += 1
            if not hit:
                self._misses += 1
        if added == 0:
            return
        self._obs += added
        self._since_search += added
        self._since_refine += added
        self._maybe_search()

    def record_observe(
        self,
        cmd_alt: float,
        cmd_az: float,
        assignments: Dict[int, str],
        target_altaz: Dict[str, Tuple[float, float]],
        hits: Iterable[str],
    ) -> None:
        """Convenience wrapper: build records from a real observe result.

        Parameters
        ----------
        cmd_alt, cmd_az:
            The pointing that was ACTUALLY SENT to the telescope (i.e. after any
            compensation already applied). Units: degrees.
        assignments:
            ``{fiber_id(int): target_id(str)}`` exactly as issued.
        target_altaz:
            ``{target_id: (alt_deg, az_deg)}`` of each assigned target at exposure
            start (from ``radec_to_altaz``).
        hits:
            Iterable of target_ids the engine reported in ``last_result["hits"]``
            (membership is purely geometric: landed on its own fibre's glass).
        """
        if not self.enabled:
            return
        hit_set = set(hits)
        records: List[Record] = []
        for fiber, tid in assignments.items():
            ta = target_altaz.get(tid)
            if ta is None:
                continue
            off = tangent_offsets(ta[0], ta[1], cmd_alt, cmd_az)
            if off is None:
                continue
            records.append((off[0], off[1], int(fiber), tid in hit_set))
        self.add_observation(records)

    # ------------------------------------------------------------------ query
    def compensate(self, alt: float, az: float) -> Tuple[float, float]:
        """Reverse-compensate a desired pointing.

        Returns ``(alt - learned_d_alt, az - learned_d_az)`` once converged, so the
        real centre lands at ``(alt, az)``. Before convergence (or when disabled) it
        returns the input unchanged -- exactly the old behaviour.
        """
        if not self.enabled or not self.converged or self.learned is None:
            return float(alt), float(az)
        oa, oe = self.learned
        new_alt = max(0.0, min(90.0, float(alt) - oa))
        new_az = float(az) - oe
        new_az = new_az % 360.0
        return new_alt, new_az

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "converged": self.converged,
            "learned_d_alt_deg": (self.learned[0] if self.learned else None),
            "learned_d_az_deg": (self.learned[1] if self.learned else None),
            "records": len(self._records),
            "misses": self._misses,
            "half_steps": self._half_steps,
        }

    # ------------------------------------------------------------------ search
    def _coarse_step(self) -> float:
        return self.pitch / _COARSE_DIV

    def _predict_hit(self, x: float, y: float, fiber: int, oa: float, oe: float) -> bool:
        """Would a target at record offset (x, y), assigned to `fiber`, have hit under
        a candidate bias (oa, oe)?"""
        X = x - oa
        Y = y - oe
        half = self.fov / 2.0
        if abs(X) > half or abs(Y) > half:
            return False
        inv_pitch = 1.0 / self.pitch
        half_side = self.side / 2.0
        row = math.floor(X * inv_pitch + half_side)
        col = math.floor(Y * inv_pitch + half_side)
        if row < 0:
            row = 0
        elif row >= self.side:
            row = self.side - 1
        if col < 0:
            col = 0
        elif col >= self.side:
            col = self.side - 1
        pred_fiber = row * self.side + col
        if pred_fiber != fiber:
            return False
        # on-glass check (gap=0 cards: always true inside FOV; general cards use the
        # glass margin, matching agent_core.geometry.FiberGrid.classify).
        cn, ce = self.grid.fiber_center(pred_fiber)
        margin = self.glass / 2.0 - max(abs(X - cn), abs(Y - ce))
        return margin >= 0.0

    def _search_grid(self, half_steps: int, step: float):
        """Score every candidate bias on the grid [-half_steps, half_steps]^2.

        Returns ``(best, best_score, runner_up)`` where ``best`` is
        ``(oa, oe, i, j)`` or None.
        """
        recs = self._records
        best = None
        best_score = -1
        runner = -1
        for i in range(-half_steps, half_steps + 1):
            oa = i * step
            for j in range(-half_steps, half_steps + 1):
                oe = j * step
                correct = 0
                for x, y, fiber, hit in recs:
                    pred = self._predict_hit(x, y, fiber, oa, oe)
                    if pred == hit:
                        correct += 1
                if correct > best_score:
                    runner = best_score
                    best_score = correct
                    best = (oa, oe, i, j)
                elif correct > runner:
                    runner = correct
        return best, best_score, runner

    def _maybe_search(self) -> None:
        # Not enough evidence yet: stay unconverged (compensate is identity).
        if self._misses < OFFSET_MIN_MISSES:
            return
        if self._since_search < OFFSET_REFINE_EVERY and self.converged:
            return
        self._since_search = 0

        step = self._coarse_step()
        best, best_score, runner = self._search_grid(self._half_steps, step)
        if best is None:
            return
        oa, oe, i, j = best

        # Auto-expand when the optimum sits on the search window edge (the real bias
        # may be larger than the nominal window).
        at_edge = (abs(i) == self._half_steps or abs(j) == self._half_steps)
        if at_edge and self._expansions < _MAX_EXPANSIONS:
            self._expansions += 1
            self._half_steps += OFFSET_STEPS
            self.log(f"offset: best on grid edge, expanding window to +/-{self._half_steps} steps")
            best, best_score, runner = self._search_grid(self._half_steps, step)
            if best is None:
                return
            oa, oe, i, j = best

        margin = best_score - runner
        if not self.converged:
            if margin >= OFFSET_MARGIN:
                self.converged = True
                self.learned = (oa, oe)
                self.log(f"offset: converged d_alt={oa:+.4f} d_az={oe:+.4f} "
                         f"(explained {best_score}/{len(self._records)}, margin {margin})")
            return

        # Already converged: only refine once we have enough evidence, and only on a
        # fine lattice around the current best.
        if self._obs < OFFSET_FINE_EVIDENCE:
            return
        if self._since_refine < OFFSET_REFINE_EVERY:
            return
        self._since_refine = 0
        fine_step = step / _FINE_DIV
        c_oa, c_oe = self.learned
        # Fine search around current best on a small window.
        half = _FINE_WINDOW * 2  # in fine-step units: +/- 2 coarse steps
        best_f = None
        best_f_score = -1
        for fi in range(-half, half + 1):
            foa = c_oa + fi * fine_step
            for fj in range(-half, half + 1):
                foe = c_oe + fj * fine_step
                correct = 0
                for x, y, fiber, hit in self._records:
                    pred = self._predict_hit(x, y, fiber, foa, foe)
                    if pred == hit:
                        correct += 1
                if best_f_score:
                    best_f_score = correct
                    best_f = (foa, foe)
        if best_f is not None:
            self.learned = best_f
            self.log(f"offset: refined d_alt={best_f[0]:+.4f} d_az={best_f[1]:+.4f} "
                     f"(explained {best_f_score}/{len(self._records)})")
