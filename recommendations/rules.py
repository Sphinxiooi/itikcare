"""Pure forward-chaining rule logic for the prescriptive module (itikcare-spec.md section 6).

Kept free of Django/ORM imports — like ``forecasting/pipeline.py`` — so the rules can be
unit-tested as plain functions over dicts. ``recommendations/engine.py`` is the thin
orchestration layer that pulls a Forecast's data from the DB, calls into here, and persists
Recommendation rows.

Threshold design (see itikcare-spec.md section 6 and CLAUDE.md's "no black box" requirement):

* Every threshold below is a **fixed named constant**, not a percentile computed at runtime —
  the same reading always produces the same verdict regardless of what else is in the database,
  which is what makes the rule traceable and defensible in a thesis defense.
* The constants were chosen by looking at ``ItikCare_Cleaned_Dataset.csv``'s observed distribution
  (n=551 rows) so they sit at realistic, non-arbitrary points in this farm's own data, not just
  generic textbook numbers. See the comment above each constant for the percentile it corresponds
  to. They are intentionally grouped here in one place so an adviser can review/adjust the whole
  table without touching any rule logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from recommendations.models import Recommendation

Priority = Recommendation.Priority

# --- Temperature (heat stress) -----------------------------------------------------------
# Observed: p25=27.3 median=28.6 p75=29.7 p90=30.5 max=34.4 degrees C.
HEAT_MODERATE_C = 30.0  # ~p75-p90: heat stress is starting to bite.
HEAT_SEVERE_C = 32.5  # near the top of the observed range: clear heat stress.

# --- Humidity (compounds heat stress) ----------------------------------------------------
# Observed: p25=74 median=78 p75=84 p90=87 max=95 percent.
HUMIDITY_MODERATE_PCT = 80.0  # ~p75.
HUMIDITY_SEVERE_PCT = 88.0  # ~p90+.

# --- Feed intake, per bird (raw kg/day is meaningless without flock size) ----------------
# Observed per-bird feed (feed_intake_kg / flock_size): min=0.152 p25=0.154 median=0.168
# p75=0.181 max=0.230 kg/bird/day. The low end of this dataset lines up closely with the
# commonly cited ~150g/bird/day minimum ration for laying ducks.
FEED_MODERATE_KG_PER_BIRD = 0.160
FEED_SEVERE_KG_PER_BIRD = 0.150

# --- Flock age (non-linear: rises to a peak ~28-29 weeks, then plateaus/declines) --------
# Peak window is documented in itikcare-spec.md section 4. POST_PEAK_DECLINE_WEEKS adds a
# buffer past the peak's end so the 28-35 week plateau fires nothing (there is nothing
# actionable while yield is still near its peak).
PEAK_AGE_START_WEEKS = 28
POST_PEAK_DECLINE_WEEKS = 35


@dataclass(frozen=True)
class Rule:
    """One IF-THEN rule: a condition over the current inputs, and what to say if it fires."""

    condition: Callable[[dict], bool]
    priority: str
    message: Callable[[dict], str]


@dataclass(frozen=True)
class FiredRule:
    """A rule that matched, flattened to exactly what a Recommendation row needs."""

    feature: str
    priority: str
    message: str


def _feed_per_bird(inputs: dict) -> float:
    return inputs["feed_intake_kg"] / inputs["flock_size"]


# Each feature's rules are ordered most-severe-first, so evaluate_rules() stops at the
# first (strongest) match and never fires both a "moderate" and "severe" recommendation
# for the same feature.
RULES: dict[str, list[Rule]] = {
    "temperature_c": [
        Rule(
            condition=lambda i: i["temperature_c"] >= HEAT_SEVERE_C,
            priority=Priority.HIGH,
            message=lambda i: (
                f"Temperature is {i['temperature_c']:.1f}°C, at or above the severe "
                f"heat-stress threshold of {HEAT_SEVERE_C:.1f}°C. Provide shade, "
                "increase ventilation/airflow, and ensure ducks have constant access to "
                "cool water — heat stress at this level can sharply reduce egg yield."
            ),
        ),
        Rule(
            condition=lambda i: i["temperature_c"] >= HEAT_MODERATE_C,
            priority=Priority.MEDIUM,
            message=lambda i: (
                f"Temperature is {i['temperature_c']:.1f}°C, above the moderate "
                f"heat-stress threshold of {HEAT_MODERATE_C:.1f}°C. Monitor ducks for "
                "signs of heat stress and improve shade/ventilation if temperatures keep "
                "climbing."
            ),
        ),
    ],
    "humidity_pct": [
        Rule(
            condition=lambda i: i["humidity_pct"] >= HUMIDITY_SEVERE_PCT,
            priority=Priority.HIGH,
            message=lambda i: (
                f"Humidity is {i['humidity_pct']:.0f}%, at or above the severe threshold of "
                f"{HUMIDITY_SEVERE_PCT:.0f}%. High humidity compounds heat stress — "
                "improve airflow/ventilation in the housing area as a priority."
            ),
        ),
        Rule(
            condition=lambda i: i["humidity_pct"] >= HUMIDITY_MODERATE_PCT,
            priority=Priority.MEDIUM,
            message=lambda i: (
                f"Humidity is {i['humidity_pct']:.0f}%, above the moderate threshold of "
                f"{HUMIDITY_MODERATE_PCT:.0f}%. Keep housing well-ventilated, especially if "
                "temperature is also elevated."
            ),
        ),
    ],
    "feed_intake_kg": [
        Rule(
            condition=lambda i: _feed_per_bird(i) < FEED_SEVERE_KG_PER_BIRD,
            priority=Priority.HIGH,
            message=lambda i: (
                f"Feed intake is {_feed_per_bird(i) * 1000:.0f}g/bird/day, below the severe "
                f"underfeeding threshold of {FEED_SEVERE_KG_PER_BIRD * 1000:.0f}g/bird/day. "
                "Increase feed ration promptly — sustained underfeeding at this level "
                "will depress egg yield."
            ),
        ),
        Rule(
            condition=lambda i: _feed_per_bird(i) < FEED_MODERATE_KG_PER_BIRD,
            priority=Priority.MEDIUM,
            message=lambda i: (
                f"Feed intake is {_feed_per_bird(i) * 1000:.0f}g/bird/day, below the "
                f"recommended {FEED_MODERATE_KG_PER_BIRD * 1000:.0f}g/bird/day. Consider "
                "increasing the feed ration to support yield."
            ),
        ),
    ],
    "flock_age_weeks": [
        Rule(
            condition=lambda i: i["flock_age_weeks"] > POST_PEAK_DECLINE_WEEKS,
            priority=Priority.MEDIUM,
            message=lambda i: (
                f"Flock age is {i['flock_age_weeks']} weeks, well past the peak laying "
                f"window (~{PEAK_AGE_START_WEEKS}-29 weeks). Gradual yield decline at this "
                "age is expected and age-related, not a husbandry problem — consider "
                "flock renewal/succession planning if this generation is approaching "
                "retirement."
            ),
        ),
        Rule(
            condition=lambda i: i["flock_age_weeks"] < PEAK_AGE_START_WEEKS,
            priority=Priority.LOW,
            message=lambda i: (
                f"Flock age is {i['flock_age_weeks']} weeks, approaching the peak laying "
                f"window (~{PEAK_AGE_START_WEEKS}-29 weeks). Maintain current feeding and "
                "environmental care — yield is expected to keep rising toward peak."
            ),
        ),
    ],
}


def evaluate_rules(inputs: dict, importance_order: list[str]) -> list[FiredRule]:
    """Forward-chain over ``importance_order``, firing at most one rule per feature.

    ``importance_order`` is the Forecast's features sorted by RF feature importance,
    descending (including features with no rules here, e.g. flock_size/lag1/roll3 -- those
    are simply skipped). Evaluating in this order is what guarantees the highest-importance
    negative factor is flagged first, per spec section 6.
    """
    fired = []
    for feature in importance_order:
        for rule in RULES.get(feature, []):
            if rule.condition(inputs):
                fired.append(FiredRule(feature=feature, priority=rule.priority, message=rule.message(inputs)))
                break
    return fired
