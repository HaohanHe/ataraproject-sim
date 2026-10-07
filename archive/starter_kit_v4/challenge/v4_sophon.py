"""Optional, private Sophon practice-card schedule and score override.

Ordinary v4 cards never load this module's schedule. The generated JSONL belongs in
``truth/``; no glyph or trigger time is included in participant messages.
"""

from __future__ import annotations

import csv
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from .contracts import derive_stream_seed
from .v4_scorer import FORCE_CLOSE_TYPE, ZERO_SCORE_TYPE, TargetScore, program_band, program_multiplier

REFERENCE_KEYS = ("seeing_arcsec", "transparency", "sky_quality", "instrument_efficiency",
                  "airmass", "lunar_factor")


def validate_config(config: Mapping, phase: str) -> None:
    """Keep the score override confined to an intentionally marked practice card."""
    if phase != "practice":
        raise ValueError("Sophon can only be enabled on a practice card")
    try:
        boost = float(config["multiplier"])
        values = [float(config["reference"][key]) for key in REFERENCE_KEYS]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid Sophon multiplier or reference conditions") from exc
    if not math.isfinite(boost) or boost <= 1 or any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("Sophon multiplier must exceed one and reference values must be positive")


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _range(value: object, name: str) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2 or any(type(item) is not int or item < 0 for item in value):
        raise ValueError(f"sophon.{name} must be two non-negative integers")
    low, high = value
    if low > high:
        raise ValueError(f"sophon.{name} is reversed")
    return low, high


def _mask(rows: object) -> list[int]:
    if not isinstance(rows, list) or len(rows) != 5 or any(
        not isinstance(row, str) or len(row) != 5 or set(row) - {"#", "."} for row in rows
    ):
        raise ValueError("sophon glyphs must be five 5-character rows of # and .")
    # Authored top-to-bottom; fiber 0 is at the bottom-left.
    return [row * 5 + col for row in range(5) for col in range(5) if rows[4 - row][col] == "#"]


def generate_schedule(spec: Mapping, calendar_csv: Path, slots_csv: Path, weather_truth_csv: Path,
                      output: Path, seed: int) -> list[dict]:
    """Fix all glyph nights with a separate, hashed RNG stream."""
    message = str(spec["message"])
    glyphs = spec["glyphs"]
    if not message or any(symbol not in glyphs for symbol in message):
        raise ValueError("sophon.message must use defined glyphs")
    masks = {symbol: _mask(rows) for symbol, rows in glyphs.items()}
    if any(not masks[symbol] for symbol in message):
        raise ValueError("sophon.message contains an empty glyph")
    first = _range(spec["first_delay_nights"], "first_delay_nights")
    gap = _range(spec["gap_nights"], "gap_nights")
    rng = random.Random(derive_stream_seed(seed, "v4.sophon.schedule"))
    with weather_truth_csv.open(newline="", encoding="utf-8") as handle:
        open_slots = {row["slot_id"] for row in csv.DictReader(handle) if row["is_observable"] == "true"}
    slots_by_night: dict[str, list[datetime]] = {}
    with slots_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["slot_id"] in open_slots:
                slots_by_night.setdefault(row["night_id"], []).append(_utc(row["timestamp_utc"]))
    with calendar_csv.open(newline="", encoding="utf-8") as handle:
        nights = [row for row in csv.DictReader(handle) if slots_by_night.get(row["night_id"])]
    index = rng.randint(*first)
    schedule = []
    round_index = 0
    while index + len(message) <= len(nights):
        for offset, symbol in enumerate(message):
            night = nights[index + offset]
            onset = _utc(night["observing_start_utc"])
            if round_index == 0 and offset == 0:
                # Leave at least half the open slots as chances to discover the first glyph.
                choices = slots_by_night[night["night_id"]]
                onset = rng.choice(choices[:max(1, len(choices) // 2)])
            schedule.append({"night_id": night["night_id"], "active_from_utc": _utc_text(onset),
                             "fiber_ids": masks[symbol]})
        round_index += 1
        index += len(message) + rng.randint(*gap)
    if not schedule:
        raise ValueError("Sophon observation period cannot fit one full message")
    output.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in schedule), encoding="utf-8")
    return schedule


def load_schedule(path: Path, slots: list, n_fibers: int) -> dict[str, tuple[datetime, frozenset[int]]]:
    if n_fibers != 25:
        raise ValueError("Sophon requires 25 fibers")
    bounds = {}
    for slot in slots:
        night_id = slot.slot_id.split("-S", 1)[0]
        if night_id not in bounds:
            bounds[night_id] = [slot.start_utc, slot.end_utc]
        else:
            bounds[night_id][1] = slot.end_utc
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        night_id = row["night_id"]
        onset = _utc(row["active_from_utc"])
        fibers = row["fiber_ids"]
        if night_id not in bounds or night_id in result or not bounds[night_id][0] <= onset < bounds[night_id][1]:
            raise ValueError("invalid Sophon night or onset")
        if not isinstance(fibers, list) or not fibers or any(type(fid) is not int or not 0 <= fid < 25 for fid in fibers) or len(set(fibers)) != len(fibers):
            raise ValueError("invalid Sophon fiber_ids")
        result[night_id] = (onset, frozenset(fibers))
    if not result:
        raise ValueError("empty Sophon schedule")
    return result


def window_fully_open(segments: list, events: list, start: datetime, end: datetime, target_altaz_at) -> bool:
    """Require every part of an exposure to have an open window for the flash."""
    cursor = start
    for seconds, truth in segments:
        end = cursor + timedelta(seconds=seconds)
        if truth is None or not truth.is_observable:
            return False
        cuts = {cursor, end}
        for event in events:
            if cursor < event.actual_start_utc < end:
                cuts.add(event.actual_start_utc)
            if cursor < event.actual_end_utc < end:
                cuts.add(event.actual_end_utc)
        boundaries = sorted(cuts)
        for left, right in zip(boundaries, boundaries[1:]):
            piece = left
            while piece < right:
                piece_end = min(right, piece + timedelta(seconds=120))
                sample = piece + (piece_end - piece) / 2
                alt, az = target_altaz_at(sample)
                if any(event.actual_start_utc <= sample < event.actual_end_utc
                       and event.covers_altaz(alt, az)
                       and (event.force_close or event.event_type in (ZERO_SCORE_TYPE, FORCE_CLOSE_TYPE))
                       for event in events):
                    return False
                piece = piece_end
        cursor = end
    return bool(segments) and cursor == end


def reference_score(target: Mapping, duration: int, program: str, score_config: Mapping,
                    sophon_config: Mapping) -> tuple[TargetScore, float]:
    """Cap completion first, then multiply the fully assembled reference score."""
    ref = sophon_config["reference"]
    values = [float(ref[key]) for key in REFERENCE_KEYS]
    seeing, transparency, sky, efficiency, airmass, lunar = values
    q = efficiency * transparency * sky * lunar / seeing / airmass ** float(score_config["airmass_exponent"]) / float(score_config["q0"])
    factor = min(float(target["feature_flux"]) * duration * q /
                 (float(score_config["flux_zero_point"]) * float(score_config["exposure_zero_point_seconds"])), 1.0)
    mult = program_multiplier(program, program_band(q, score_config), score_config)
    boost = float(sophon_config["multiplier"])
    return TargetScore(str(target["target_id"]), factor, q,
                       boost * float(target["science_weight"]) * factor * mult), mult
