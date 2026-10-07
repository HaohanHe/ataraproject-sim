"""Band-level fitting from saturated hits (mechanism S4).

The legacy planner declares a program by estimating the per-target band quality as
``model * state.scale / 0.95``, where ``state.scale`` is the median of
``factor * f0t0 / (flux * duration * model)`` taken only from *unsaturated* hits.
That ratio conflates the sky/transparency/seeing term (which determines the program
band) with the instrument-efficiency term (which does *not* — see
``v4_scorer.band_quality`` vs ``score_target_exposure``: the former excludes
``instrument_efficiency_multiplier``, the latter includes it).  When the instrument
degrades, ``state.scale`` drops and the agent under-declares.

This module uses *saturated* hits (``factor == 1``, so ``score/weight`` is exactly the
declared program's multiplier) to bound the band-relevant sky quality
``band_sky = q_band / model`` independently of instrument efficiency.

Constraint-satisfaction design
-------------------------------
Each saturated match tells us the *actual* band was the declared program P.  That
places a hard interval on ``band_sky`` for that hit:

  * DARK  : band_sky >= DARK_THRESHOLD / model
  * BRIGHT: BRIGHT_THRESHOLD / model <= band_sky < DARK_THRESHOLD / model
  * BACKUP: 0 <= band_sky < BRIGHT_THRESHOLD / model

Over the recent window we take ``L = max`` of all lower bounds and
``U = min`` of all upper bounds.  The feasible band-sky range is ``[L, U)``:

  * If ``L < U`` the data is self-consistent.  We return ``L * BAND_OPT`` — the
    optimistic edge of the feasible range — so that targets whose geometry puts them
    just above the DARK boundary are declared DARK rather than BRIGHT.
  * If ``L >= U`` the window mixes incompatible sky conditions (e.g. a DARK hit and
    a BACKUP hit within 2 h).  We return ``None`` so the planner falls back to the
    legacy ``state.scale / 0.95`` rule rather than forcing a wrong program.

This avoids the earlier mistake of treating the band boundary as a *point* estimate
(which under-estimated band_sky by ~25 % on a DARK night, since actual q_band median
~0.84 vs boundary 0.65).  The max/min aggregation also handles varying target
geometries (different ``model`` values) correctly: a high-airmass DARK hit
(model=0.8) demands ``band_sky >= 0.65/0.8 = 0.81``, which is what a new
lower-airmass target (model=1.0) then gets used as ``1.0 * 0.81 = 0.81 -> DARK``.

Only the Python standard library is used.

Environment switch
------------------
``OBS_ENABLE_BANDFIT``: ``"1"`` (default) = on, ``"0"`` = off.
"""
from __future__ import annotations

import os
from typing import Optional

# -- tunables (mirror python-pro BAND_* constants) ------------------------------
BAND_MEMORY_HOURS = 2.0      # sliding window for saturated samples used in the fit
BAND_OPT = 1.0               # optimism factor applied on declaration (1.0 = neutral)
BAND_HALF_LIFE = 0.0         # 0 = equal weights (reserved)
BAND_FALLBACK = 0.0          # reserved: fixed fallback level (0 = "return None")
BAND_FALLBACK_HOURS = 12.0   # hard prune: samples older than this are discarded
BAND_CONT = 0.0              # 0 = no continuity tie-break

# Tolerance for "score/weight ≈ declared multiplier" — matches state.py's 2e-4.
_SAT_TOL = 2e-4

# Minimum number of saturated hits in the window before we trust the constraints.
_MIN_SAMPLES = 2

# Per-program constraints on band_sky = q_band / model.
# DARK:  band_sky >= DARK_TH / model
# BRIGHT: BRIGHT_TH / model <= band_sky < DARK_TH / model
# BACKUP: 0 <= band_sky < BRIGHT_TH / model
# Thresholds are read from ScoringModel at runtime; defaults match scoring.py.
_DEFAULT_DARK_TH = 0.65
_DEFAULT_BRIGHT_TH = 0.40


class BandFitter:
    """Aggregate saturated hits into a feasible band-sky interval."""

    def __init__(self, scoring, env: Optional[dict] = None) -> None:
        self.scoring = scoring
        self._env = env if env is not None else os.environ
        self.enabled = str(self._env.get("OBS_ENABLE_BANDFIT", "0")) != "0"

        # Saturated samples: (hours, lower_bound, upper_bound).
        # bounds are on band_sky = q_band / model.
        self._sat: list[tuple[float, float, float]] = []
        # Unsaturated ratios: (hours, ratio) for E estimation.
        self._unsat: list[tuple[float, float]] = []
        self._last_level: Optional[float] = None

    # -- ingestion ---------------------------------------------------------------

    def on_result(
        self,
        hours: float,
        *,
        score: float,
        weight: float,
        flux: float,
        duration: float,
        model: float,
        declared_program: str,
        clean: bool,
    ) -> None:
        """Feed one target hit from ``state.on_result``."""
        if not self.enabled:
            return
        if score <= 0.0 or weight <= 0.0 or flux <= 0.0:
            return
        if duration <= 0 or model <= 0.0:
            return
        if not clean:
            return

        multipliers = self.scoring.program_multipliers
        mult = multipliers.get(declared_program, 1.0)
        ratio_sw = score / weight

        if abs(ratio_sw - mult) <= _SAT_TOL:
            # -- saturated match: record the feasible interval on band_sky --------
            bands = self.scoring.program_bands
            dark_th = float(bands.get("DARK", _DEFAULT_DARK_TH))
            bright_th = float(bands.get("BRIGHT", _DEFAULT_BRIGHT_TH))
            if declared_program == "DARK":
                lo = dark_th / model
                hi = float("inf")
            elif declared_program == "BRIGHT":
                lo = bright_th / model
                hi = dark_th / model
            else:  # BACKUP
                lo = 0.0
                hi = bright_th / model
            self._sat.append((hours, lo, hi))
        else:
            # -- unsaturated: record ratio for E estimation ----------------------
            mismatch = self.scoring.mismatch_multiplier
            f_match = ratio_sw / mult if mult > 0 else 0.0
            f_miss = ratio_sw / mismatch if mismatch > 0 else 0.0
            factor = f_match if 0.0 < f_match < 0.97 else f_miss
            if factor <= 0.0 or factor >= 0.97:
                return
            f0t0 = self.scoring.f0t0
            ratio = factor * f0t0 / (flux * duration * model)
            if ratio > 0.0:
                self._unsat.append((hours, ratio))

    # -- queries ----------------------------------------------------------------

    def _prune(self, hours: float) -> None:
        cutoff_hard = hours - BAND_FALLBACK_HOURS
        self._sat = [t for t in self._sat if t[0] >= cutoff_hard]
        self._unsat = [t for t in self._unsat if t[0] >= cutoff_hard]

    def band_scale(self, hours: float) -> Optional[float]:
        """Feasible band-sky level (after BAND_OPT) for ``_finish_plan``.

        Returns ``None`` when disabled, too few samples, or the constraints are
        inconsistent (L >= U — sky changed mid-window).  Otherwise returns the
        optimistic edge of the feasible interval multiplied by ``BAND_OPT``.
        """
        if not self.enabled:
            return None
        self._prune(hours)
        cutoff = hours - BAND_MEMORY_HOURS
        recent = [(lo, hi) for h, lo, hi in self._sat if h >= cutoff]
        if len(recent) < _MIN_SAMPLES:
            return None

        L = max(lo for lo, _ in recent)
        U = min(hi for _, hi in recent)

        if L >= U:
            # Inconsistent — sky conditions changed during the window.
            return None

        # Pick the optimistic edge.  If L == 0 (only BACKUP hits), pick the
        # midpoint of [0, U) so we don't return exactly 0.
        if L <= 0.0:
            level = U * 0.5
        else:
            level = L

        if BAND_CONT > 0.0 and self._last_level is not None:
            level = (1.0 - BAND_CONT) * level + BAND_CONT * self._last_level
        self._last_level = level
        return level * BAND_OPT

    def instrument_efficiency(self, hours: float) -> Optional[float]:
        """Estimate ``E = q_exp / q_band`` from recent unsaturated hits."""
        if not self.enabled:
            return None
        self._prune(hours)
        band_sky = self._last_level
        if band_sky is None:
            # Try to compute from current saturated constraints.
            cutoff = hours - BAND_MEMORY_HOURS
            recent = [(lo, hi) for h, lo, hi in self._sat if h >= cutoff]
            if len(recent) < _MIN_SAMPLES:
                return None
            L = max(lo for lo, _ in recent)
            U = min(hi for _, hi in recent)
            if L >= U:
                return None
            band_sky = L if L > 0.0 else U * 0.5
            self._last_level = band_sky
        cutoff = hours - BAND_MEMORY_HOURS
        ratios = sorted(r for h, r in self._unsat if h >= cutoff)
        if len(ratios) < _MIN_SAMPLES:
            return None
        med_ratio = ratios[len(ratios) // 2]
        return med_ratio / band_sky
