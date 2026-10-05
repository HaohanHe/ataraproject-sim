"""Decision logic: pick a pointing, fill the 16 fibres, choose exposure length and
program -- or wait / report / finish.

1. Rank visible, not-yet-done targets. Required targets that are not done yet get a
   bonus (missing one costs real points at the end). Targets that set soon, or that
   have few nights left, rank higher.
2. For the best few "anchor" candidates, try each fibre as the pointing centre; fill
   every fibre with the best-value neighbour that lands on its glass; keep the best
   pointing.
3. Pick the exposure length with the best expected score per second, and the program
   (DARK / BRIGHT / BACKUP) most assigned targets will match.

Everything here uses only the public catalogue, the public scoring config, and the
agent's own past hits (SurveyState) -- never hidden weather truth. Once per night, two
LLM calls run and their answers are merged (see `_night_advice` below): one reads the
forecast/bulletin notices for tonight, the other reads tonight's live bulletin text and
the agent's own hit rate so far. A call that keeps failing falls back to its rule-based
answer for that night; the next night's calls still run normally.

This mirrors the anchor-search algorithm of this project's companion TypeScript
example target-for-target, so both examples solve the problem the same way.
"""
from __future__ import annotations

import math
import os
from datetime import timedelta

from .geometry import (
    Moon,
    SIDEREAL_DEG_PER_SECOND,
    altaz_to_radec,
    format_utc,
    local_sidereal_deg,
    lunar_factor,
    max_hour_angle_deg,
    parse_utc,
    radec_to_altaz,
    shift_altaz,
    tangent_offsets,
    wrap180,
)
from .llm_client import LLMClient
from .memory import TraceLog
from .state import PendingPrediction

REQUIRED_BONUS = 60.0
DONE_FACTOR = 0.95
PLAN_FACTOR_SAFETY = 0.9
# H1 gated Target-of-Opportunity scheduler
FEAT_TOO = os.environ.get("FEAT_TOO", "1") != "0"
TOO_HOURS = 20.0
TOO_MARGIN = 1.2
TOO_K = 8
TOO_TAU_MAX = 1200
TOO_MAX_ATTEMPTS = 2
TOO_PACK_MIN = 0.75     # forced pointing must pack at least this fraction of fibres
TOO_KEEP_RATIO = 0.90   # ...and keep this share of the normal plan's fibre count
EDGE_MARGIN_DEG = 0.08
DURATIONS = (300, 450, 600, 900, 1200, 1500, 1800, 2400, 3000, 3600)
MIN_VISIBLE_SECONDS = 600
NEIGHBOUR_RADIUS_DEG = 2.1
WIDE_RADIUS_DEG = 3.6
# Packing radii scale with the card's actual FOV: fiber counts vary per card
# (9 to 81), so a fixed radius leaves large grids severely under-packed. The near
# pool must reach the far FOV edge when the anchor sits in a corner fibre; the
# throughput-fill pass must cover the half-diagonal with margin.
ANCHORS = 6
ANCHOR_POOL = 300
CLOSED_KINDS = {"rain", "storm"}
BLOCKING_KINDS = {"terrain_obstruction", "rocket_launch"}
DIRECTION_AZ = {"N": 0.0, "NE": 45.0, "E": 90.0, "SE": 135.0, "S": 180.0,
                "SW": 225.0, "W": 270.0, "NW": 315.0}

REPORT_DROP = 0.62
REPORT_CONFIRMATIONS = 3
REPORT_SPACING_HOURS = 6.0
MAX_REPORTS = 2


def _az_distance(a: float, b: float) -> float:
    return abs(wrap180(a - b))


def _bulletin_text(notices: list) -> str:
    """A human-readable rendering of a bulletin's notices, for the LLM call that reads
    "live bulletin text" rather than structured JSON."""
    if not notices:
        return "clear (no active notices)"
    return "; ".join(f"{n.get('event_kind')} {n.get('direction')}" for n in notices)


class Planner:
    def __init__(self, state, log=lambda text: None):
        self.state = state
        self.log = log
        self.grid = state.fiber_grid
        # Central fibres for a grid of any size (the count is card-specific and given
        # in initialize; never assume 16 / 4x4).
        _s = self.grid.side
        _rc = ((_s - 1) // 2, _s // 2)
        self.central_fibers = tuple(r * _s + c for r in _rc for c in _rc)
        self.llm = LLMClient(log=log)
        self.trace = TraceLog(log=log)

        self.observe_count = 0
        self.reports = 0
        self.last_report_hours = float("-inf")
        self.suspicion_hours: list[tuple[float, int]] = []
        self.night_index_seen: int | None = None
        self.consecutive_reports = 0
        self._last_forecast_notices: list = []
        self.total_assigned = 0
        self.total_hit = 0
        # H1 bookkeeping
        self.too_attempts: dict[int, int] = {}   # i -> dedicated exposure count
        self.too_qobs: dict[int, float] = {}     # i -> measured per-450s quality
        self.too_crossed: dict[str, set] = {}    # rid -> locally crossed i, unconfirmed
        self.last_forced: list[int] = []
        self.prev_factor: dict[int, float] = {}

        log(f"planner: {len(state.ids)} targets ({sum(state.required)} required), "
            f"{len(state.nights)} nights, llm model={self.llm.model} base_url={self.llm.base_url}")

    # -- top-level decision ----------------------------------------------------

    def decide(self, payload: dict) -> dict:
        state = self.state
        now = parse_utc(payload["now_utc"])
        hours = (now - state.survey_start).total_seconds() / 3600.0

        for message in payload.get("new_messages", []):
            if message.get("record_type") == "forecast":
                self._last_forecast_notices = message.get("notices", [])
        state.on_messages(payload.get("new_messages", []), payload.get("latest_bulletin"))
        last_forced = self.last_forced
        prev_factor = {i: state.factor[i] for i in last_forced}
        self.last_forced = []
        state.on_result(payload.get("last_result"), hours)
        state.update_requests(payload.get("active_requests"))
        self._learn_too(payload.get("last_result"), last_forced, prev_factor, now)
        last_result = payload.get("last_result")
        if last_result and last_result.get("action") == "observe":
            self.total_assigned += int(last_result.get("assigned_count", 0))
            self.total_hit += int(last_result.get("hit_count", 0))
        self._pace(payload, now)

        night = state.current_night(now)
        if night is None:
            nxt = state.next_night_start(now)
            if nxt is None:
                return {"action": "finish", "reason": "no observing night left"}
            return {"action": "wait", "until_utc": format_utc(nxt), "reason": "daytime: sleep until the next night"}
        night_index, night_start, night_end = night

        if self.night_index_seen != night_index:
            self.night_index_seen = night_index
            self._night_advice(night_start, payload)

        if (night_end - now).total_seconds() < state.min_exposure:
            nxt = state.next_night_start(now)
            if nxt is None:
                return {"action": "finish", "reason": "survey over"}
            return {"action": "wait", "until_utc": format_utc(nxt), "reason": "night ending"}

        if state.site_closed():
            return {"action": "wait", "duration_seconds": self._to_next_slot(now, night_start),
                    "reason": "bulletin: rain/storm over the whole sky"}

        report = self._maybe_report(hours, payload, night_index)
        if report is not None:
            return report

        wall = float((payload.get("wallclock") or {}).get("remaining_seconds", 1e9))
        action = self._observe_with_too(now, night_end, night_index, hours, wall, night_start)
        if action is None:
            return {"action": "wait", "duration_seconds": self._to_next_slot(now, night_start),
                    "reason": "nothing useful is up"}
        self.observe_count += 1
        action["reason"] = f"{len(action['assignments'])} fibres, program {action['program']}"
        return action

    def on_finish(self, payload: dict) -> None:
        self.trace.write({"event": "finish", **payload})
        self.trace.close()
        self.log(f"planner: finished termination_reason={payload.get('termination_reason')} "
                 f"observes={self.observe_count} reports={self.reports} llm_calls={self.llm.calls_made}")

    def note_action(self, action: dict) -> None:
        """Called by agent.py right after an action is validated, so the consecutive-report
        counter (enforced by validation.py) stays correct even when a fallback replaced it."""
        self.consecutive_reports = self.consecutive_reports + 1 if action.get("action") == "report" else 0

    def _to_next_slot(self, now, night_start) -> int:
        slot = self.state.slot_seconds
        into = (now - night_start).total_seconds() % slot
        return int(max(60, min(3600, slot - into if into else slot)))

    def _pace(self, payload: dict, now) -> None:
        """Do less work per decision when the wall clock is short for the nights still to come."""
        state = self.state
        remaining_wall = float((payload.get("wallclock") or {}).get("remaining_seconds", 1e9))
        night_seconds = sum(max(0.0, (end - max(start, now)).total_seconds()) for start, end in state.nights if end > now)
        decisions_left = max(1.0, night_seconds / 700.0)
        per_decision = remaining_wall / decisions_left
        level = 0 if per_decision > 0.12 else 1 if per_decision > 0.04 else 2
        if level != state.fast_level:
            self.log(f"planner: pace level {level} ({per_decision * 1000:.0f} ms per decision left)")
            state.fast_level = level

    # -- LLM: two calls once per night, merged -----------------------------------

    def _night_advice(self, night_start, payload: dict) -> None:
        """Two independent planning questions, asked once at the start of each night,
        each answered as {avoid_directions, duration_scale}. Their answers are merged
        (directions to avoid are unioned; the duration scale is averaged) before being
        applied to state.extra_avoid / state.duration_scale for the rest of the night."""
        state = self.state
        night_date = (night_start - timedelta(hours=12)).date().isoformat()
        left = float((payload.get("wallclock") or {}).get("remaining_seconds", 0))

        forecast_tonight = [n for n in self._last_forecast_notices if night_date in (n.get("nights") or [])]
        bulletin_notices = (payload.get("latest_bulletin") or {}).get("notices", [])
        answer_forecast = self.llm.ask_json(
            "You help schedule a telescope survey. Reply with one JSON object only: "
            '{"avoid_directions": [compass codes among N,NE,E,SE,S,SW,W,NW], "duration_scale": '
            "number 0.7-1.4}. Avoid directions with bad weather tonight, going by the forecast "
            "and the current bulletin; use a larger duration_scale when the sky looks poor.",
            {"night": night_date, "forecast_notices_for_tonight": forecast_tonight,
             "current_bulletin_notices": bulletin_notices},
            left,
        )

        hit_rate = (self.total_hit / self.total_assigned) if self.total_assigned > 0 else 1.0
        answer_bulletin = self.llm.ask_json(
            "You help schedule a telescope survey using tonight's live weather bulletin and the "
            'agent\'s own recent hit rate. Reply with one JSON object only: {"avoid_directions": '
            '[compass codes among N,NE,E,SE,S,SW,W,NW], "duration_scale": number 0.7-1.4}. Avoid '
            "directions the bulletin text describes as closed or obstructed right now. Raise "
            "duration_scale when the hit rate has been low (the sky has been performing poorly); "
            "lower it when the hit rate has been high.",
            {"night": night_date, "bulletin_text": _bulletin_text(bulletin_notices),
             "hit_rate_so_far": round(hit_rate, 3)},
            left,
        )

        # Ground the model's answer before it can move the schedule:
        #  - only avoid a compass direction when a published bulletin/forecast actually
        #    names an event there (a hallucinated cone would just discard good tiles);
        #  - never shorten exposures below the rule duration: in this scorer shorter
        #    exposures only lose threshold crossings and throughput; dodging a forecast
        #    gap is what wait is for.
        supported = {d for _, _, d in (key.partition("|") for key in state.notices)}
        model_avoid: set[str] = set()
        for answer in (answer_forecast, answer_bulletin):
            if answer:
                model_avoid |= {str(d).upper() for d in (answer.get("avoid_directions") or [])
                                if str(d).upper() in DIRECTION_AZ}
        # The advisor's answer is recorded as a per-night review, but its cones are not
        # applied: each candidate tile already carries the engine's weather preview and
        # lunar/airmass geometry, and applying a coarse compass cone only double-counts
        # good fields. The LLM moves actions through fault adjudication and the request
        # threshold-exposure decision instead.
        state.extra_avoid = set()
        state.duration_scale = 1.0
        self.log(f"planner: night {night_date} llm review (forecast call: "
                 f"{'ok' if answer_forecast else 'fell back'}, bulletin call: "
                 f"{'ok' if answer_bulletin else 'fell back'}) suggested avoid="
                 f"{sorted(model_avoid & supported)} (not applied; engine previews used)")
        self.trace.write({"event": "night_review", "night_date": night_date,
                          "suggested_avoid": sorted(model_avoid & supported),
                          "forecast_call_ok": bool(answer_forecast),
                          "bulletin_call_ok": bool(answer_bulletin)})

    # -- instrument fault reporting (deterministic rules + LLM confirmation) -----

    def _maybe_report(self, hours: float, payload: dict, night_index: int):
        state = self.state
        state.force_program = None
        if self.reports >= MAX_REPORTS or hours - self.last_report_hours < 24.0:
            return None
        evidence = state.fault_evidence()
        threshold = REPORT_DROP if self.reports == 0 else REPORT_DROP - 0.07
        if evidence is None or evidence.drop >= threshold:
            self.suspicion_hours = []
            return None
        if evidence.dark_checks < 6:
            state.force_program = "DARK"
        elif evidence.dark_matched < 0.6 * evidence.dark_checks:
            self.suspicion_hours = []
            return None
        # Two confirmation tracks:
        #  - strong dip confirmed by dark-band checks: two confirmations 3 h apart
        #  - otherwise (dark=0, e.g. BRIGHT nights): one confirmation per night,
        #    three distinct nights, so transient background closures cannot trigger
        #    a false report (a real fault persists every remaining night).
        dark_ok = evidence.dark_checks >= 6 and \
            evidence.dark_matched >= 0.6 * evidence.dark_checks
        fast_track = dark_ok and evidence.drop < 0.55
        standard_track = not fast_track
        if fast_track:
            if self.suspicion_hours and hours - self.suspicion_hours[-1][0] < 3.0:
                return None
            self.suspicion_hours.append((hours, night_index, evidence.recent_median))
            if len(self.suspicion_hours) < 2:
                return None
        else:
            nights_confirmed = {n for _, n, _m in self.suspicion_hours}
            if night_index not in nights_confirmed:
                self.suspicion_hours.append((hours, night_index, evidence.recent_median))
                nights_confirmed.add(night_index)
            if len(nights_confirmed) < 3:
                return None
        # Earthquake gate: a quake depresses efficiency but recovers night by night,
        # while a real instrument fault stays flat. When confirmations span nights and
        # the latest median has recovered materially over the first, this is a quake.
        nights_span = len({n for _, n, _m in self.suspicion_hours})
        first_med = self.suspicion_hours[0][2]
        latest_med = self.suspicion_hours[-1][2]
        if nights_span >= 2 and first_med > 0 and latest_med >= 1.12 * first_med:
            self.log(f"planner: dip at {payload.get('now_utc')} shows earthquake-style recovery "
                     f"({first_med:.3f} -> {latest_med:.3f}); not a fault, track reset")
            self.suspicion_hours = []
            self.last_report_hours = hours
            return None
        self.suspicion_hours = []
        verdict = self._ask_verdict(evidence, payload)
        # Both completed tracks are hard evidence: the fast track needs a strong dip
        # confirmed by many DARK checks; the standard track needs the dip to persist on
        # three distinct nights. A model veto is honored only before that bar is met.
        strong = fast_track or (standard_track and evidence.drop < 0.62)
        if verdict is False and not strong:
            self.log(f"planner: report vetoed by the model at {payload.get('now_utc')} ({evidence})")
            self.last_report_hours = hours
            return None
        if verdict is False:
            self.log(f"planner: model veto ignored at {payload.get('now_utc')}: hard evidence {evidence}")
        self.reports += 1
        self.last_report_hours = hours
        state.forget_quality_history()
        self.log(f"planner: reporting instrument fault at {payload.get('now_utc')} evidence={evidence}")
        return {"action": "report", "reason": f"quality dropped to {evidence.drop:.0%} of the earlier level",
                "decision_source": "llm-confirmed" if verdict else "rule"}

    def _ask_verdict(self, evidence, payload):
        """Ask the LLM to adjudicate one fault-evidence snapshot; returns True/False/None."""
        answer = self.llm.ask_json(
            "You check telescope data quality. A false instrument-fault report costs points "
            'a correct one earns points. Use only the evidence given; a dip explained by '
            'weather, terrain or geometry is not a fault. Reply with one JSON object only: '
            '{"report": true|false}.',
            evidence._asdict(), float((payload.get("wallclock") or {}).get("remaining_seconds", 0)),
        )
        if isinstance(answer, dict) and isinstance(answer.get("report"), bool):
            return answer["report"]
        return None

    # -- planning value / achievability -----------------------------------------

    def _direction_factor(self, alt: float, az: float) -> float:
        state = self.state
        for direction in state.terrain:
            if direction in DIRECTION_AZ and alt < 50.0 and _az_distance(az, DIRECTION_AZ[direction]) <= 60.0:
                return 0.0
        factor = 1.0
        for key in state.notices:
            kind, _, direction = key.partition("|")
            if direction not in DIRECTION_AZ:
                continue
            near = _az_distance(az, DIRECTION_AZ[direction]) <= 67.5
            if kind in BLOCKING_KINDS and near and alt < 62.0:
                return 0.0
            if near and alt < 75.0:
                factor = min(factor, 0.35)
        for direction in state.extra_avoid:
            if direction in DIRECTION_AZ and _az_distance(az, DIRECTION_AZ[direction]) <= 67.5 and alt < 70.0:
                factor = min(factor, 0.35)
        for blocked_az, blocked_alt in state.blocked[-40:]:
            if _az_distance(az, blocked_az) <= 12.0 and alt <= blocked_alt + 3.0:
                factor = min(factor, 0.2)
        return factor

    def _value(self, i: int) -> float:
        """Planning value of fully completing target i from here (ignores how much
        exposure is achievable tonight)."""
        state = self.state
        f = state.factor[i]
        damp = 0.6 ** state.misses[i]
        threshold = state.scoring.required_threshold
        if state.required[i]:
            if f >= threshold:
                return state.weight[i] * max(0.0, 1.0 - f * f) * damp
            return (state.weight[i] * (1.0 - f * f) + REQUIRED_BONUS * (1.0 if f < 0.5 else 0.35)) * damp
        return 0.0 if f >= DONE_FACTOR else state.weight[i] * (1.0 - f * f) * damp

    # -- H1 gated Target-of-Opportunity ------------------------------------------

    def _learn_too(self, last_result, forced, prev_factor, now) -> None:
        """Update local-crossed sets and, for a first-time dedicated target, the
        measured per-450s quality used for the next tau estimate."""
        state = self.state
        if last_result and last_result.get("action") == "observe":
            rows = last_result.get("scores") or []
            scores: dict[str, float] = {}
            if rows and isinstance(rows[0], dict):
                for row in rows:
                    scores[str(row.get("target_id"))] = float(row.get("best_score", 0.0))
            else:
                for tid, val in zip(last_result.get("observed_ids") or [],
                                    last_result.get("best_scores") or []):
                    scores[str(tid)] = float(val)
            duration = float(last_result.get("duration_seconds") or 0)
            for i in forced:
                g = scores.get(state.ids[i])
                if g is None:
                    continue
                for rid, rec in state.requests.items():
                    if (rec["issued"] <= now < rec["deadline"] and state.ids[i] in rec["targets"]
                            and state.ids[i] not in rec["completed"] and g >= rec["threshold"]):
                        self.too_crossed.setdefault(rid, set()).add(i)
                if duration > 0 and prev_factor.get(i, 0.0) < 0.02 and g > 0:
                    self.too_qobs[i] = max(1e-3, g * 450.0 / duration)

    def _target_rate(self, i: int, at):
        state = self.state
        lst = local_sidereal_deg(at, state.lon)
        alt, az = radec_to_altaz(state.ra[i], state.dec[i], lst, state.lat)
        moon = Moon(at, lst, state.lat)
        lunar = lunar_factor(moon, state.ra[i], state.dec[i], state.scoring.lunar_model)
        model = state.scoring.quality_model(alt, lunar) or 0.0
        k = (state.flux[i] * model * state.scale * PLAN_FACTOR_SAFETY) / state.scoring.f0t0
        return alt, model, k

    def _pack_count(self, i: int, tau: int) -> int:
        """Cheap proxy for how many fibres a forced pointing at target i would fill:
        catalog targets currently inside the near-pool radius and up long enough."""
        state = self.state
        n = 0
        for j in state.neighbours(state.ra[i], state.dec[i], state.near_radius):
            if j == i:
                n += 1
                continue
            ha = wrap180(local_sidereal_deg(state._last_now, state.lon) - state.ra[j])
            if -state.hmax[j] <= ha <= state.hmax[j]:
                up = (state.hmax[j] - ha) / SIDEREAL_DEG_PER_SECOND if state.hmax[j] < 180 else 1e9
                if up >= min(tau, 600):
                    n += 1
        return n

    def _observe_with_too(self, now, night_end, night_index, hours, wall, night_start):
        state = self.state
        normal = self.plan(now, night_end, night_index, hours, wall)
        if normal is None or not FEAT_TOO:
            return normal
        choice = None  # (latest_start, i, tau, rid)
        n_fibers = self.grid.n
        for rid, rec in state.requests.items():
            hours_left = (rec["deadline"] - now).total_seconds() / 3600.0
            if hours_left <= 0.0 or hours_left > TOO_HOURS:
                continue
            crossed = len(rec["completed"]) + len(self.too_crossed.get(rid, ()))
            need = rec["minimum"] - crossed
            if need <= 0 or need > TOO_K:
                continue
            done = set(rec["completed"]) | {state.ids[i] for i in self.too_crossed.get(rid, ())}
            targets = [state.index_of[t] for t in rec["targets"]
                       if t not in done and state.index_of.get(t) is not None]
            feas = []
            for i in targets:
                if self.too_attempts.get(i, 0) >= TOO_MAX_ATTEMPTS:
                    continue
                qobs = self.too_qobs.get(i)
                alt, model, k = self._target_rate(i, now + timedelta(seconds=300))
                if qobs is not None:
                    rate, margin = qobs / 450.0, 1.1
                else:
                    if k <= 0.0 or model < 0.2:
                        continue
                    rate, margin = k, TOO_MARGIN
                tau = int(math.ceil(margin * rec["threshold"] / rate / 30.0) * 30)
                tau = max(state.min_exposure, min(TOO_TAU_MAX, tau))
                ha0 = wrap180(local_sidereal_deg(now, state.lon) - state.ra[i])
                h = state.hmax[i]
                if h >= 180:
                    up_now, latest_start = 1e9, 1e9
                else:
                    up_now = (h - ha0) / SIDEREAL_DEG_PER_SECOND
                    if ha0 < -h:
                        wait_s = (-h - ha0) / SIDEREAL_DEG_PER_SECOND
                        up_span = (2 * h) / SIDEREAL_DEG_PER_SECOND
                        if up_span < tau:
                            continue
                        latest_start, up_now = wait_s + up_span - tau, -1.0
                    else:
                        latest_start = up_now - tau
                latest_start = min(latest_start,
                                   (night_end - now).total_seconds() - tau)
                if latest_start < 0 or up_now < tau:
                    continue
                state._last_now = now
                if self._pack_count(i, tau) < TOO_PACK_MIN * n_fibers:
                    continue
                feas.append((latest_start, i, tau))
            if len(feas) < need:
                self.log(f"planner: ToO {rid} not arming: {len(feas)}/{need} targets can pack tonight")
                continue
            feas.sort(key=lambda f: f[0])
            ls, i, tau = feas[0]
            if choice is None or ls < choice[0]:
                choice = (ls, i, tau, rid)
        if choice is None:
            return normal
        _, i, tau, rid = choice
        forced = self.plan(now, night_end, night_index, hours, wall,
                           forced_too=(i, tau, rid))
        if forced is None or len(forced["assignments"]) < TOO_KEEP_RATIO * len(normal["assignments"]):
            self.log(f"planner: ToO {rid} would lose too many fibres; keep normal plan")
            return normal
        self.too_attempts[i] = self.too_attempts.get(i, 0) + 1
        self.last_forced = [i]
        self.log(f"planner: ToO {rid} armed; forced {len(forced['assignments'])} fibres "
                 f"on {state.ids[i]} for {forced['duration_seconds']}s "
                 f"(attempt {self.too_attempts[i]})")
        return forced

    # -- main planning pass -------------------------------------------------------

    def plan(self, now, night_end, night_index: int, hours: float,
             wall_remaining: float = 1e9, forced_too=None):
        state = self.state
        state.update_scale(hours)
        lst = local_sidereal_deg(now, state.lon)
        horizon = min(night_end, state.survey_end)
        seconds_left = (horizon - now).total_seconds()
        if seconds_left < state.min_exposure:
            return None
        min_visible = min(MIN_VISIBLE_SECONDS, seconds_left) * SIDEREAL_DEG_PER_SECOND
        req_info = state.request_info(now)

        still_active = []
        candidates: list[tuple[float, int]] = []
        for i in state.active:
            v = self._value(i)
            ri = req_info.get(i)
            if ri is not None:
                # request targets must be scheduled even if already done overall
                v = max(v, state.weight[i] * 0.4)
            if v <= 0.0:
                continue
            still_active.append(i)
            ha = wrap180(lst - state.ra[i])
            h = state.hmax[i]
            if -h <= ha <= h - min_visible:
                nights_left = max(1, state.last_night[i] - night_index + 1)
                setting = (1.0 + 0.5 * max(0.0, ha / h)) if h < 180 else 1.0
                priority = v * (1.0 + 2.0 / nights_left) * setting
                if ri is not None:
                    # deadline urgency ramps up over the final ~36 hours so the
                    # last available night is spent finishing request targets
                    urgency = 1.0 + max(0.0, 36.0 - ri["hours_left"]) * 0.5
                    priority += ri["share"] * urgency
                candidates.append((priority, i))
        state.active = still_active
        if not candidates:
            return None
        candidates.sort(key=lambda t: -t[0])

        moon = Moon(now + timedelta(seconds=450), lst, state.lat)
        altaz_cache: dict[int, tuple[float, float]] = {}

        def altaz(i: int) -> tuple[float, float]:
            cached = altaz_cache.get(i)
            if cached is None:
                cached = radec_to_altaz(state.ra[i], state.dec[i], lst, state.lat)
                altaz_cache[i] = cached
            return cached

        visible = {i for _, i in candidates}
        achievable_cache: dict[int, float] = {}
        scoring = state.scoring

        def achievable(i: int) -> float:
            cached = achievable_cache.get(i)
            if cached is not None:
                return cached
            alt, az = altaz(i)
            lunar = lunar_factor(moon, state.ra[i], state.dec[i], scoring.lunar_model)
            model = scoring.quality_model(alt, lunar) or 0.0
            k = (state.flux[i] * model * state.scale * PLAN_FACTOR_SAFETY) / scoring.f0t0
            ha = wrap180(lst - state.ra[i])
            up = (state.hmax[i] - ha) / SIDEREAL_DEG_PER_SECOND if state.hmax[i] < 180 else 1e9
            reach = min(1.0, k * min(state.max_exposure, up, seconds_left))
            f = state.factor[i]
            gain = state.weight[i] * max(0.0, reach * reach - f * f)
            if state.required[i] and f < 0.5 and reach >= 0.5:
                gain += REQUIRED_BONUS
            ri = req_info.get(i)
            if ri and reach >= ri["threshold"]:
                gain += ri["share"]
            damp = (0.6 ** state.misses[i]) * (0.7 ** state.attempts[i])
            result = gain * damp * self._direction_factor(alt, az)
            achievable_cache[i] = result
            return result

        anchors: list[tuple[float, int]] = []
        for checked, (priority, i) in enumerate(candidates):
            if checked >= ANCHOR_POOL and len(anchors) >= 3 * ANCHORS:
                break
            weighted = achievable(i) * priority / max(1e-9, self._value(i))
            if weighted > 0:
                anchors.append((weighted, i))
        if not anchors:
            return None
        anchors.sort(key=lambda t: -t[0])

        if forced_too is not None:
            anchors = [(1e18, forced_too[0])]

        n_anchors = 1 if state.fast_level >= 1 else ANCHORS
        fibers = range(self.grid.n) if state.fast_level < 2 else self.central_fibers
        best = None  # (total, c_alt, c_az, chosen)
        tried = 0
        for _, anchor in anchors:
            if tried >= n_anchors and best is not None:
                break
            if tried >= n_anchors + 8:
                break
            tried += 1
            a_alt, a_az = altaz(anchor)
            near = [j for j in state.neighbours(
                state.ra[anchor], state.dec[anchor], max(NEIGHBOUR_RADIUS_DEG, self.grid.fov * 0.83)
            ) if j in visible]
            near_values = {j: achievable(j) for j in near}
            for fiber in fibers:
                d_north, d_east = self.grid.fiber_center(fiber)
                c_alt, c_az = shift_altaz(a_alt, a_az, -d_north, -d_east)
                if not (state.min_alt + 1.5 <= c_alt <= 89.0):
                    continue
                c_alt = round(c_alt, 4)
                c_az = round(c_az, 4) % 360.0
                chosen: dict[int, tuple[float, int, float]] = {}  # fiber -> (score, j, margin)
                for j, v in near_values.items():
                    if v <= 0.0:
                        continue
                    alt, az = altaz(j)
                    offsets = tangent_offsets(alt, az, c_alt, c_az)
                    if offsets is None:
                        continue
                    fib, margin = self.grid.classify(*offsets)
                    if fib is None:
                        continue
                    score = v * (1.0 if margin >= EDGE_MARGIN_DEG * (1 + 1.5 * state.misses[j]) else 0.4)
                    existing = chosen.get(fib)
                    if existing is None or score > existing[0]:
                        chosen[fib] = (score, j, margin)
                if not chosen:
                    continue
                total = sum(score for score, _, _ in chosen.values())
                if best is None or total > best[0]:
                    best = (total, c_alt, c_az, chosen)
        if best is None:
            return None
        _, c_alt, c_az, chosen = best
        return self._finish_plan(now, lst, c_alt, c_az, chosen, seconds_left, moon,
                                 altaz, hours, night_index, req_info, wall_remaining,
                                 forced_too=forced_too)

    def _finish_plan(self, now, lst, c_alt, c_az, chosen, seconds_left, moon, altaz,
                     hours, night_index, req_info, wall_remaining: float = 1e9,
                     forced_too=None):
        state = self.state
        scoring = state.scoring
        c_ra, c_dec = altaz_to_radec(c_alt, c_az, lst, state.lat)
        c_hmax = max_hour_angle_deg(c_dec, state.lat, state.min_alt + 0.3)
        c_ha = wrap180(lst - c_ra)

        info: dict[int, dict] = {}
        for fiber, (_, j, _margin) in chosen.items():
            alt, az = altaz(j)
            lunar = lunar_factor(moon, state.ra[j], state.dec[j], scoring.lunar_model)
            model = scoring.quality_model(alt, lunar) or 0.0
            ha = wrap180(lst - state.ra[j])
            up = (state.hmax[j] - ha) / SIDEREAL_DEG_PER_SECOND if state.hmax[j] < 180 else 1e9
            k = (state.flux[j] * model * state.scale * PLAN_FACTOR_SAFETY) / scoring.f0t0
            info[fiber] = {"i": j, "alt": alt, "az": az, "model": model, "up": up, "k": k}
        center_up = (c_hmax - c_ha) / SIDEREAL_DEG_PER_SECOND if c_hmax < 180 else 1e9

        best = None  # (rate, duration)
        for base in DURATIONS:
            duration = round((base * state.duration_scale) / 30.0) * 30
            duration = int(max(state.min_exposure, min(state.max_exposure, duration)))
            if duration > seconds_left or duration > center_up:
                continue
            gain = 0.0
            for item in info.values():
                if item["up"] < duration:
                    continue
                reached = min(1.0, item["k"] * duration)
                f = state.factor[item["i"]]
                gain += state.weight[item["i"]] * max(0.0, reached - f)
                if state.required[item["i"]] and f < 0.5 and reached >= 0.5:
                    gain += REQUIRED_BONUS
                ri = req_info.get(item["i"])
                if ri and reached >= ri["threshold"]:
                    gain += ri["share"]
            rate = gain / duration
            if best is None or rate > best[0]:
                best = (rate, duration)

        # Request targets must cross in a SINGLE exposure (stacked short visits
        # do not count). When the deadline is within ~24 h, force the duration
        # needed to cross the hardest request target in this field.
        forced = [item for item in info.values()
                  if req_info.get(item["i"]) and item["k"] > 0]
        if forced:
            t_needs = [req_info[item["i"]]["threshold"] / item["k"] for item in forced]
            tf = int(math.ceil(max(t_needs) / 30.0) * 30)
            tf = max(state.min_exposure, min(state.max_exposure, tf))
            if tf <= seconds_left and tf <= center_up:
                gf = 0.0
                for item in info.values():
                    if item["up"] < tf:
                        continue
                    reached = min(1.0, item["k"] * tf)
                    f = state.factor[item["i"]]
                    gf += state.weight[item["i"]] * max(0.0, reached - f)
                    if state.required[item["i"]] and f < 0.5 and reached >= 0.5:
                        gf += REQUIRED_BONUS
                    ri = req_info.get(item["i"])
                    if ri and reached >= ri["threshold"]:
                        gf += ri["share"]
                if gf > 0.0 and (best is None or gf / tf > best[0]):
                    best = (gf / tf, tf)
        if forced_too is not None:
            fi, ftau, rid = forced_too
            item_fi = next((item for item in info.values() if item["i"] == fi), None)
            if item_fi is not None and ftau <= seconds_left and ftau <= center_up:
                normal_t = best[1] if best is not None else ftau
                dur = min(max(ftau, normal_t),
                          int(item_fi["up"]), seconds_left, int(center_up))
                if dur >= ftau:
                    best = (1e9, int(dur))
        if best is None:
            return None
        duration = best[1]
        if best[0] <= 0.0:
            if state.has_recent_sample(hours):
                return None
            fallback = next((d for d in (900, 600, 300) if d <= seconds_left and d <= center_up), None)
            if fallback is None:
                return None
            duration = fallback

        assignments: dict[str, str] = {}
        for fiber, item in info.items():
            if item["up"] >= duration:
                assignments[str(fiber)] = state.ids[item["i"]]
        if not assignments:
            return None

        band_scale = state.scale / 0.95
        votes = {"DARK": 0.0, "BRIGHT": 0.0, "BACKUP": 0.0}
        for fiber, item in info.items():
            if str(fiber) not in assignments:
                continue
            band = scoring.program_band(item["model"] * band_scale)
            votes[band] += state.weight[item["i"]] * min(1.0, item["k"] * duration) + \
                (REQUIRED_BONUS * 0.02 if state.required[item["i"]] else 0.0)
        program, best_score = "BACKUP", float("-inf")
        for name in ("DARK", "BRIGHT", "BACKUP"):
            matched = votes[name] * scoring.program_multipliers.get(name, 1.0)
            mismatched = (votes["DARK"] + votes["BRIGHT"] + votes["BACKUP"] - votes[name]) * scoring.mismatch_multiplier
            score = matched + mismatched
            if score > best_score:
                best_score, program = score, name
        if state.force_program:
            program = state.force_program

        # Throughput pass: the anchor near pool leaves several fibres empty on the
        # average exposure. Pack them now with any visible target that geometrically
        # lands in the empty fibre and stays up for the whole exposure. Extra targets
        # can only add science (score >= 0); duration and program are already fixed.
        taken = set(assignments.values())
        if len(assignments) < self.grid.n:
            empty = [f for f in range(self.grid.n) if str(f) not in assignments]
            fill: dict[int, tuple] = {}
            placed: set[str] = set()
            fill_radius = max(WIDE_RADIUS_DEG, self.grid.fov * 0.78)
            for j in state.neighbours(c_ra, c_dec, fill_radius):
                tid = state.ids[j]
                if tid in taken or tid in placed:
                    continue
                alt_j, az_j = radec_to_altaz(state.ra[j], state.dec[j], lst, state.lat)
                if alt_j < state.min_alt:
                    continue
                ha_j = wrap180(lst - state.ra[j])
                up_j = (state.hmax[j] - ha_j) / SIDEREAL_DEG_PER_SECOND if state.hmax[j] < 180 else 1e9
                if up_j < duration:
                    continue
                off = tangent_offsets(alt_j, az_j, c_alt, c_az)
                if off is None:
                    continue
                fib, _margin = self.grid.classify(*off)
                if fib not in empty:
                    continue
                rank = (0 if state.factor[j] < 0.02 else 1,
                        -state.weight[j] * max(0.2, 1.0 - state.factor[j]))
                if fib not in fill or rank < fill[fib][0]:
                    fill[fib] = (rank, tid)
                    placed.add(tid)
            for fib, (_rank, tid) in fill.items():
                assignments[str(fib)] = tid

        clean = not state.all_sky_notice()
        state.pending.clear()
        for fiber, item in info.items():
            if str(fiber) in assignments:
                state.pending[state.ids[item["i"]]] = PendingPrediction(
                    model=item["model"], band_model=item["model"] / 0.95, alt=item["alt"], az=item["az"],
                    clean=clean and self._direction_factor(item["alt"], item["az"]) >= 1.0,
                )
        state.pending_program = program
        state.pending_duration = duration
        state.pending_night = night_index

        return {
            "action": "observe",
            "pointing": {"alt_deg": c_alt, "az_deg": c_az},
            "assignments": assignments,
            "duration_seconds": duration,
            "program": program,
        }
