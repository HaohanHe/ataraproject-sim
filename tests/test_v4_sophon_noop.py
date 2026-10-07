"""Ordinary (non-Sophon) v4 cards score byte-identically with the Sophon branch present.

The digests below were produced by main 33f9a50 (before challenge/v4_sophon.py existed) with the
deterministic probe agent: the public demo card and a generated stress card. Any change to them
means the Sophon change touched ordinary scoring.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


from challenge.v4_bundle import build_card_bundle
from challenge.v4_probe_agent import make_agent
from challenge.v4_runner import run_scenario

ROOT = Path(__file__).resolve().parents[1]
STRESS_SPEC = {"name": "noop-stress", "card_id": "NOOP", "scenario_slug": "noop-stress", "phase": "practice",
               "seed": 20261005, "start_date": "2026-10-01", "end_date": "2026-10-06", "targets": 800,
               "area_deg2": 600.0, "stress": True, "wallclock_seconds": 300}
GOLDEN = {
    "demo": {
        "decisions.csv": "8c0ab78fa11087962e094355e1937a6a2e2ef8134b6e1c9cf65c057a2acd659d",
        "messages.jsonl": "764321eaabddf3c96d4a7136d7f61f026a3c6a68d6e1cce7c8b4cc16e53a6f2c",
        "observations.csv": "97afa60f9012a9348288a15855f4b5ccd6a168340bdb2d2605e639e26ded3ef0",
        "score_report.json": "37b246b10dcac5627d79c782eff0f4cba3dc55c35174854ad0903a8a4ce73733",
    },
    "stress": {
        "decisions.csv": "5af8c9ebb6b2c233e4be09540c5bf730e84860d580a42bc1bab30e79ed0fcd1d",
        "messages.jsonl": "7e4f6a629a6f4a57c51b343191b6f31091529d6a70eb97032d49b19fd5151137",
        "observations.csv": "51e3d40133038870e5f933d61ca427e6189f07624220cbc519b8793a31e134af",
        "score_report.json": "2bc580227ea6fc0f2d248842e279a785e43de80b41c5ddaf5062ed6b7499ef75",
    },
}


def _digests(scenario: Path, out: Path) -> dict:
    run_scenario(scenario, make_agent, out)
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir())}


def test_demo_card_scores_byte_identically(tmp_path):
    scenario = ROOT / "archive/starter_kit_v4/cards/demo/config/v4_scenario.json"
    assert _digests(scenario, tmp_path / "out") == GOLDEN["demo"]


def test_stress_card_scores_byte_identically(tmp_path):
    bundle = build_card_bundle(tmp_path / "card", STRESS_SPEC)
    assert _digests(bundle / "config/v4_scenario.json", tmp_path / "out") == GOLDEN["stress"]
