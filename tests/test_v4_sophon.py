"""Sophon is an opt-in practice card; ordinary v4 scoring remains unchanged."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone

import pytest

from challenge.v4_bundle import REFERENCE, SPEC_CONFIGS, build_spec_bundle
from challenge.v4_fiber_map import min_altitude_during, radec_to_altaz
from challenge.v4_runner import load_scenario, run_scenario
from challenge.v4_scorer import ScoreEvent, SlotTruth
from challenge.v4_sophon import reference_score, window_fully_open
from challenge.v4_workflow import V4Workflow


def _card(tmp_path):
    spec = tmp_path / "spec"
    spec.mkdir()
    configs = {name: json.loads((REFERENCE / name).read_text()) for name in SPEC_CONFIGS}
    for name in ("v4_catalog_config.json", "v4_weather_config.json"):
        configs[name]["seed"] = 42
        configs[name]["seed_derivation"] = "sha256-v1"
    catalog = configs["v4_catalog_config.json"]
    weather = configs["v4_weather_config.json"]
    catalog["observability"].update(start_date="2026-10-01", end_date="2026-10-13")
    weather["survey"].update(start_date="2026-10-01", end_date="2026-10-13")
    catalog["targets"]["total_count"] = 1500
    catalog["footprint"].update(total_area_deg2=900, n_components=2,
                                 component_area_weights=[0.6, 0.4], vertices_per_component=24)
    configs["v4_fiber_config.json"]["field"]["n_fibers"] = 25
    for name, config in configs.items():
        (spec / name).write_text(json.dumps(config))
    card = {"name": "test-sophon", "card_id": "TEST-SOPHON", "scenario_slug": "test-sophon",
            "phase": "practice", "stress": False, "observation_requests": {"count": 0},
            "sophon": {"message": "T", "glyphs": {"T": ["#####", "..#..", "..#..", "..#..", "..#.."]},
                       "multiplier": 2.5, "first_delay_nights": [0, 0], "gap_nights": [0, 0],
                       "reference": {"seeing_arcsec": 1, "transparency": 1, "sky_quality": 1,
                                     "instrument_efficiency": 1, "airmass": 1, "lunar_factor": 1}}}
    (spec / "card.json").write_text(json.dumps(card))
    return build_spec_bundle(tmp_path / "bundle", spec)


def test_sophon_private_schedule_and_score_override(tmp_path):
    bundle = _card(tmp_path)
    scenario_path = bundle / "config" / "v4_scenario.json"
    scenario = load_scenario(scenario_path)
    assert scenario.fiber_config["field"]["n_fibers"] == 25
    assert len(scenario.sophon_schedule) == 12
    assert all(mask == frozenset({2, 7, 12, 17, 20, 21, 22, 23, 24})
               for _, mask in scenario.sophon_schedule.values())
    assert "sophon" not in V4Workflow(bundle).initialize_payload(900)
    assert not (bundle / "public" / "v4_sophon.jsonl").exists()

    choice = None
    for slot in scenario.slots:
        night = slot.slot_id.split("-S", 1)[0]
        onset = scenario.sophon_schedule[night][0]
        if slot.start_utc < onset or not slot.is_observable:
            continue
        for target in scenario.targets:
            if min_altitude_during(target["ra_deg"], target["dec_deg"], slot.start_utc,
                                   slot.start_utc + timedelta(seconds=900), scenario.config) < 30:
                continue
            alt, az = radec_to_altaz(target["ra_deg"], target["dec_deg"], slot.start_utc,
                                     scenario.site["latitude_deg"], scenario.site["longitude_deg"])
            if 30 < alt < 85:
                choice = slot, target, alt, az
                break
        if choice:
            break
    assert choice is not None
    slot, target, alt, az = choice

    def factory(_context):
        steps = iter(({"action": "wait", "until_utc": slot.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")},
                      {"action": "observe", "pointing": {"alt_deg": alt, "az_deg": az},
                       "assignments": {"12": target["target_id"]}, "duration_seconds": 900,
                       "program": "DARK"}, None))
        return lambda _snapshot: next(steps)

    run_scenario(scenario_path, factory, tmp_path / "flash")
    with (tmp_path / "flash" / "observations.csv").open(newline="") as handle:
        [observed] = list(csv.DictReader(handle))
    expected, mult = reference_score(target, 900, "DARK", scenario.score_config, scenario.config["sophon"])
    assert int(observed["fiber_id"]) == 12
    assert float(observed["factor"]) == pytest.approx(expected.factor, abs=1e-6)
    assert float(observed["quality"]) == pytest.approx(expected.quality, abs=1e-6)
    assert float(observed["prog_mult"]) == pytest.approx(mult, abs=1e-6)
    assert float(observed["score"]) == pytest.approx(expected.score, abs=1e-6)

    ordinary = json.loads(scenario_path.read_text())
    ordinary.pop("sophon")
    scenario_path.write_text(json.dumps(ordinary))
    run_scenario(scenario_path, factory, tmp_path / "ordinary")
    with (tmp_path / "ordinary" / "observations.csv").open(newline="") as handle:
        [normal] = list(csv.DictReader(handle))
    assert float(normal["score"]) != pytest.approx(float(observed["score"]))


def test_sophon_window_closure_blocks_flash():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=900)
    open_slot = SlotTruth("N-S001", start, end, True, 1, 1, 1, 1)
    closed_slot = SlotTruth("N-S001", start, end, False, 0, 0, 0, 0)
    target_altaz = lambda _moment: (50.0, 315.0)
    assert window_fully_open([(900, open_slot)], [], start, end, target_altaz)
    assert not window_fully_open([(900, closed_slot)], [], start, end, target_altaz)
    closure = ScoreEvent("R", "rocket_launch", start + timedelta(seconds=300),
                         start + timedelta(seconds=600), "HORIZON_SECTOR", 300, 330, 30, 60,
                         1, 1, 1, 1, True)
    assert not window_fully_open([(900, open_slot)], [closure], start, end, target_altaz)


def test_sophon_rejected_on_non_practice_card(tmp_path):
    scenario_path = _card(tmp_path) / "config" / "v4_scenario.json"
    config = json.loads(scenario_path.read_text())
    config["task_card"]["phase"] = "final"
    scenario_path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="practice card"):
        load_scenario(scenario_path)
