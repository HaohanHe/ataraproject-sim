"""Dense-patch field candidates + pointing refinement (mechanism S2).

Faithful port of the python-pro planner additions:

* The anchor loop in :mod:`agent_core.planner` only uses individual targets as
  candidate field centres.  Dense fields add candidates that *do not* come from a
  single anchor target: all not-yet-finished science targets are aggregated on the
  sky into ``DENSE_BIN_DEG`` bins, the densest ``N_DENSE`` bin centroids become
  candidate field centres, and each centroid is aligned preferentially with the
  central fibres ``DENSE_FIBERS`` (the 2x2 middle block of a 4x4 grid).
* After the best point (anchor or dense) has been picked, ``refine`` hill-climbs the
  pointing on the tangent plane with an ``REFINE_STEP_DEG`` step over the 8
  neighbours, up to ``REFINE_ROUNDS`` rounds, keeping whichever centre packs the
  higher total.  ``REFINE_FIXED_T = 1`` means the exposure duration is held fixed
  during refinement (the caller passes a scoring callback that already assumes a
  fixed duration).

Only the public catalogue geometry is used (RA/Dec, the fibre grid and the public
alt/az transforms in :mod:`agent_core.geometry`) -- never hidden truth.  Pure
standard library, deterministic, fast: binning is one pass over the unfinished
targets and refinement is at most 4 * 8 packing callbacks.

Switch: environment variable ``OBS_ENABLE_DENSE``.  ``"0"`` turns the whole module
off (dense candidates return ``[]``, refinement returns the centre unchanged); any
other value -- the default when unset -- enables it.
"""
from __future__ import annotations

import math
import os
from typing import Callable, Optional, Sequence, Tuple

from .geometry import radec_to_altaz, shift_altaz

# -- constants ported from python-pro ------------------------------------------
DENSE_BIN_DEG = 2.5          # side of the sky bin used to aggregate patches
N_DENSE = 20                 # how of the densest bins become candidate fields
DENSE_FIBERS = (5, 6, 9, 10)  # central 2x2 fibres of a 4x4 grid (row-major)
REFINE_STEP_DEG = 0.1        # tangent-plane step used by the hill-climb
REFINE_NEIGHBOURS = 8        # 4 edge + 4 diagonal neighbours per round
REFINE_ROUNDS = 4            # maximum hill-climb rounds
REFINE_FIXED_T = 1           # flag: keep the exposure duration fixed while refining

# 8 neighbour offsets (d_north, d_east) on the tangent plane.
_REFINE_OFFSETS: Tuple[Tuple[float, float], ...] = tuple(
    (dn, de)
    for dn in (-REFINE_STEP_DEG, 0.0, REFINE_STEP_DEG)
    for de in (-REFINE_STEP_DEG, 0.0, REFINE_STEP_DEG)
    if not (dn == 0.0 and de == 0.0)
)
assert len(_REFINE_OFFSETS) == REFINE_NEIGHBOURS


def dense_enabled() -> bool:
    """Whether dense candidates / refinement are switched on (env OBS_ENABLE_DENSE)."""
    return os.environ.get("OBS_ENABLE_DENSE", "1") != "0"


def _spherical_centroid(indices: Sequence[int], ra: Sequence[float],
                         dec: Sequence[float]) -> Tuple[float, float]:
    """Centroid of targets `indices` as the mean unit vector on the sphere.

    Unlike an arithmetic RA mean this is correct across the 0/360 seam and does
    not distort near the poles.  Returns (ra_deg [0,360), dec_deg [-90,90]).
    """
    sx = sy = sz = 0.0
    for i in indices:
        r = math.radians(ra[i] % 360.0)
        d = math.radians(max(-90.0, min(90.0, dec[i])))
        cd = math.cos(d)
        sx += cd * math.cos(r)
        sy += cd * math.sin(r)
        sz += math.sin(d)
    n = len(indices)
    sx, sy, sz = sx / n, sy / n, sz / n
    dec_c = math.degrees(math.asin(max(-1.0, min(1.0, sz))))
    ra_c = math.degrees(math.atan2(sy, sx)) % 360.0
    return ra_c, dec_c


class DensePlanner:
    """Builds dense-patch candidate pointings and refines a chosen field centre.

    Parameters
    ----------
    grid:
        The fibre grid (``state.fiber_grid``); only ``grid.n`` and
        ``grid.fiber_center(fiber)`` are used here.
    log:
        Optional logging sink.
    enabled:
        Overrides the env switch for testing (``None`` -> read ``OBS_ENABLE_DENSE``).
    """

    def __init__(self, grid, log: Callable[[str], None] = lambda t: None,
                 enabled: Optional[bool] = None):
        self.grid = grid
        self.log = log
        self.enabled = dense_enabled() if enabled is None else bool(enabled)
        # Central fibres that exist on this grid (DENSE_FIBERS assume a 4x4 grid;
        # smaller/larger cards keep only the indices that are in range).
        nfib = int(getattr(grid, "n", 0))
        self.dense_fibers = tuple(f for f in DENSE_FIBERS if 0 <= f < nfib)

    # -- dense binning ---------------------------------------------------------

    def bin_centers(self, indices: Sequence[int], ra: Sequence[float],
                    dec: Sequence[float]) -> list:
        """Aggregate unfinished targets into ``DENSE_BIN_DEG`` sky bins.

        Parameters
        ----------
        indices:
            Slots (into `ra`/`dec`) of the not-yet-finished science targets the
            dense search may use -- typically the same candidate pool the anchor
            loop already built.
        ra, dec:
            Parallel RA/Dec arrays (degrees), indexed by slot.

        Returns
        -------
        list[(ra_c, dec_c, count)]
            Up to ``N_DENSE`` bin centroids, densest bin first.  Empty when the
            feature is disabled or there is nothing to aggregate.  The centroid is
            the spherical (unit-vector) mean of the bin's members, so it stays
            valid across the RA seam and near the poles.
        """
        if not self.enabled or not indices:
            return []
        b = DENSE_BIN_DEG
        nra = int(round(360.0 / b))
        cells: dict = {}
        for i in indices:
            ra_i = float(ra[i]) % 360.0
            dec_i = max(-90.0, min(90.0, float(dec[i])))
            key = (int(ra_i // b), int((dec_i + 90.0) // b))
            cells.setdefault(key, []).append(i)

        # Merge the RA-seam cells: a cluster straddling ra=0/360 must not be split
        # into two weak bins.
        merged: list = []
        consumed = set()
        for (rc, dc), members in cells.items():
            if (rc, dc) in consumed:
                continue
            if rc == 0 and (nra - 1, dc) in cells:
                combined = list(members) + list(cells[(nra - 1, dc)])
                consumed.add((0, dc))
                consumed.add((nra - 1, dc))
                merged.append(combined)
            elif rc == nra - 1 and (0, dc) in cells:
                # the rc==0 visit below performs the merge
                consumed.add((rc, dc))
            else:
                consumed.add((rc, dc))
                merged.append(members)

        merged.sort(key=len, reverse=True)
        out = []
        for members in merged[:N_DENSE]:
            ra_c, dec_c = _spherical_centroid(members, ra, dec)
            out.append((ra_c, dec_c, len(members)))
        return out

    # -- candidate pointings ----------------------------------------------------

    def candidate_pointings(self, centers: Sequence[Tuple[float, float]],
                            lst: float, lat: float, min_alt: float) -> list:
        """Turn dense bin centroids into concrete (alt/az) pointing candidates.

        For each bin centroid we place that centroid on one of ``DENSE_FIBERS`` by
        reversing the tangent shift -- exactly the same inverse geometry the
        anchor loop uses (`c = shift(anchor_alt, anchor_az, -fiber_offset)`) -- so
        the integrator can feed every returned candidate into the *same* packing
        block it already runs for an anchor.

        Returns
        -------
        list[(ra_c, dec_c, c_alt, c_az, fiber)]
            Only candidates whose field centre stays in the safe altitude window
            (``min_alt + 1.5 <= c_alt <= 89``), matching planner.py's own bounds
            check.  ``c_alt``/``c_az`` are rounded to 4 decimals and ``c_az`` is
            wrapped to [0, 360).
        """
        if not self.enabled or not centers or not self.dense_fibers:
            return []
        lo = min_alt + 1.5
        out = []
        for ra_c, dec_c, *_ in centers:
            a_alt, a_az = radec_to_altaz(ra_c, dec_c, lst, lat)
            for fiber in self.dense_fibers:
                d_north, d_east = self.grid.fiber_center(fiber)
                c_alt, c_az = shift_altaz(a_alt, a_az, -d_north, -d_east)
                if not (lo <= c_alt <= 89.0):
                    continue
                out.append((ra_c, dec_c, round(c_alt, 4), round(c_az, 4) % 360.0,
                            fiber))
        return out

    # -- pointing refinement ----------------------------------------------------

    def refine(self, c_alt: float, c_az: float,
               score_fn: Callable[[float, float], float],
               alt_lo: Optional[float] = None,
               alt_hi: float = 89.0) -> Tuple[float, float, float]:
        """Hill-climb the pointing centre to maximise ``score_fn``.

        Parameters
        ----------
        c_alt, c_az:
            Starting centre (degrees), already chosen by the anchor/dense search.
        score_fn:
            ``score_fn(alt, az) -> float``: the expected packed total for a field
            centred at (alt, az), with the exposure duration held fixed
            (``REFINE_FIXED_T = 1``).  Return a negative/``math.inf`` value (or
            ``None``) for an invalid candidate.
        alt_lo, alt_hi:
            Acceptable altitude window; neighbours leaving it are skipped
            (defaults to 0 .. 89 -- the integrator normally passes
            ``min_alt + 1.5`` to match the candidate bounds).

        Returns
        -------
        (best_alt, best_az, best_score)
            The best centre found.  When the feature is disabled the input centre
            is returned unchanged (its score is still evaluated once so the caller
            can proceed).
        """
        lo = 0.0 if alt_lo is None else alt_lo

        def _score(alt: float, az: float) -> float:
            try:
                value = score_fn(alt, az)
            except Exception:
                return float("-inf")
            if value is None:
                return float("-inf")
            return float(value)

        best_alt, best_az, best_score = c_alt, c_az, _score(c_alt, c_az)
        if not self.enabled:
            return best_alt, best_az, best_score

        for _ in range(REFINE_ROUNDS):
            moved = False
            for dn, de in _REFINE_OFFSETS:
                n_alt, n_az = shift_altaz(best_alt, best_az, dn, de)
                if not (lo <= n_alt <= alt_hi):
                    continue
                value = _score(n_alt, n_az)
                if value > best_score:
                    best_alt, best_az, best_score = n_alt, n_az, value
                    moved = True
            if not moved:
                break
        return best_alt, best_az, best_score
