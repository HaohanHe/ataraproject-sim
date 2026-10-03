# Observer BI4MIB: A Survey-Scheduling Agent Written by Someone Who Used the Moon as a "Repeater"

> Entry for the GOSIM 2026 "Agentic Observer" hackathon
> Solo contestant: He Haohan, amateur radio callsign **BI4MIB**

## 1. Opening: I have literally "used" the Moon before

Late one night in 2024, I set up a home-built Yagi antenna on a rooftop in Changchun and transmitted
radio power toward the lunar surface, 380,000 km away, waiting for the signal to bounce off the
Moon and come back to Earth — an EME (Earth-Moon-Earth) contact. Whether the contact succeeds
depends on the Moon's **position, altitude, phase, and the atmosphere along its path**: when the
Moon is low, the signal crosses a thicker slab of atmosphere and the noise floor rises; around full
moon, thermal noise from the illuminated ground degrades reception. Like a scheduler, I had to
compute moonrise and moonset ahead of time and spend every transmit window where it mattered.

So when, in this hackathon's scoring engine, I read the **lunar quality factor** — how the lunar
angle, phase, and angular separation from the Moon discount an exposure's science return — I did
not see a foreign astronomy formula. I saw something I had been writing in my radio logs for two
years.

This project is a night-shift scheduler for a simulated spectroscopic survey telescope, written by
someone who has bounced signals off the Moon.

## 2. What it is

An observer agent speaking **JSON Lines over stdin/stdout** (participant-agent-protocol-v4). Every
900 seconds the server sends a state snapshot; the agent returns one action:

- `observe`: point at an azimuth/altitude, assign the 16 fibers to targets, choose an exposure
  duration and a program (DARK / BRIGHT / BACKUP);
- `wait`: wait for targets to rise or conditions to improve;
- `report`: confirm and report an instrument fault (a correct report repairs it immediately; a
  false report costs points).

It runs with no third-party dependencies. With an OpenAI-compatible endpoint configured, the
per-night strategy advisor and the fault verdict involve an LLM (covering natural-language
understanding, task planning, action decisions, tool calls, and plan adaptation).

## 3. What I added on top of the reference observer

The organizers ship a high-quality reference observer (rule-based policy plus belief state). I used
it as the base and touched only two files (`agent_core/state.py`, `agent_core/planner.py`), adding
two capabilities.

### 3.1 Observation-request scheduling — the reference does not handle requests at all

The game issues temporary observation requests: before a deadline, obtain a **single exposure with
factor ≥ 0.5** for at least 6 of 8 named targets; reward: 100 points. Reading the engine source
line by line confirmed three mechanics:

1. Only exposures **fully contained within [issued, deadline]** count;
2. For each target, the engine keeps the **maximum factor of any single exposure** — stacked short
   exposures do **not** add up; one exposure must cross the threshold on its own;
3. The completion reward settles only after minimum_completed (6/8) targets qualify.

Implementation:

- `state.update_requests / active_requests / request_info`: normalize requests, track completed
  targets, compute each unfinished target's share (reward / minimum) and remaining time;
- when ranking candidate tiles, request targets are weighted by **deadline urgency** (ramping over
  the final 36 hours, so the last available night is actually spent finishing the request);
- a **forced single-exposure duration block**: within 24 hours of the deadline, if the field
  contains a request target, the duration needed to cross its threshold is computed and compared on
  rate — eliminating the failure mode of four busy-looking short exposures that never cross.

### 3.2 Robust fault reporting: dual-track confirmation plus post-exposure event gating

Instrument faults are unpredictable and only depress efficiency; but directional weather (cloud or
haze in one azimuth sector), rocket launches, and unpublished short background closures also crash
quality and look just like a fault. The reference's "three confirmations six hours apart" is too
slow on some cards and fires falsely on others.

My design:

- **fast track**: when the dip is strong (<0.55) and dark-band checks confirm it (≥6 DARK checks,
  ≥60% matched), two confirmations three hours apart suffice — the dark-band validation is hard
  evidence that this is not a directional weather event;
- **standard track**: when dark validation is unavailable (e.g. BRIGHT-program nights near full
  moon), require one confirmation on **three distinct nights** — a real fault persists to the end of
  the survey, while background closures clear within a night or two;
- **post-exposure directional-event gating**: for exposures that started before an event bulletin
  was published and were hit mid-exposure, results are re-checked against the latest bulletin using
  the exposure's starting direction; samples explained by a directional event are excluded from
  fault-detection history.

## 4. Local replay results (L1–L4, no LLM key)

| Card | Reference baseline | This project | Delta | Requests | Fault report |
|---|---:|---:|---:|---|---|
| L1 | 4458.56 | **4672.25** | +213.7 | 1/2 | correct +100 |
| L2 | 4767.70 | **4857.75** | +90.1 | 2/2 | correct +100 |
| L3 | 4288.30 | **4292.27** | +4.0 | 1/2 | — (fault overlaps double-earthquake recovery; confirmation gate deliberately withheld) |
| L4 | 3378.54 | **3913.33** | +534.8 | 2/2 | correct +100 |

Total across the four cards: **+842.6**, with zero false reports. Development weather is separated
from evaluation weather, and the policy relies on no known-scenario parameters — what is compared is
generalization.

## 5. How to run

```bash
# local replay (from the example pack's runner/ directory)
python3 run_local.py --card ../local-cards/L1 --agent /path/to/agent.py --out out --wallclock 800

# optional LLM advisor
export OPENAI_API_KEY=...   # or an OpenAI-compatible endpoint, see .env.example
```

`agent.py` must be executable; protocol details are in `README.md`.

## 6. Credits and license

- The competition data, mission cards, evaluation programs, and reference observer are provided by
  the GOSIM 2026 Agentic Observer Hackathon under **CC BY-NC 4.0**:
  https://creativecommons.org/licenses/by-nc/4.0/ ; please cite the GOSIM 2026 Agentic Observer
  Hackathon (https://create.gosim.org/survey26/).
- My additions on top of the reference are released under the **MIT license**; every increment was
  implemented independently from the public documentation and scoring configuration, with no
  verbatim copying of copyleft code.
- Thanks to public survey projects such as DESI for making "the observing plan shapes the science
  sample" something one can study and benchmark objectively.

— BI4MIB, 73.
