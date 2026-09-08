"""Pure forward-chaining rule logic for the prescriptive module (itikcare-spec.md section 6).

Kept free of Django/ORM imports — like ``forecasting/pipeline.py`` — so the rules can be
unit-tested as plain functions over dicts. ``recommendations/engine.py`` is the thin
orchestration layer that pulls a Forecast's data from the DB, calls into here, and persists
Recommendation rows.

Threshold source (see CLAUDE.md's "no black box" requirement):

* Every tier boundary, combination table, and message below is copied verbatim from the
  adviser-provided ``Annex-A-Rules-Table.pdf`` ("ItikCare Prescriptive Recommendations —
  Rules/Threshold Table", the validated annex that supersedes the earlier ``rules-table.pdf``
  draft) — this is now the single source of truth for the prescriptive module, replacing the
  dataset-percentile-derived thresholds this file used to carry. Anything not in that table
  (e.g. a cold-stress or dry-humidity tier this farm's
  own historical CSV never happened to record) is still implemented, because the table is
  the spec regardless of what one farm's data happened to show.
* Recommendations collapse to exactly **3 slots**, always fired (nothing silently skipped,
  same forward-chaining philosophy as before) — see ``evaluate_rules``:
  - ``feed_intake_kg`` — flock age tier x age-tiered feed tier -> one of the AF1-AF15
    combination rules (Annex A 2.1). Feed thresholds are themselves age-dependent
    (1.4), so age and feed are evaluated together, not independently.
  - ``flock_age_weeks`` — flock age tier alone -> retirement/replacement guidance
    (Annex A 2.6). A separate concern from the feed-ration advice above (this is
    about flock succession planning, not day-to-day feeding).
  - ``environment`` — temperature and humidity are evaluated *together*: each one's
    5-tier reading feeds into two matrices (2.2 "temperature flag", 2.3 "humidity flag")
    that are asymmetric (looking up (temp, humidity) is not the same as (humidity, temp)),
    producing two potentially-different resultant tiers. The worse of the two drives this
    recommendation's priority, and both matrices' recommendation text (2.4 and 2.5) are
    included in the message, so nothing either reading implies is lost by merging them into
    one recommendation. ``"environment"`` is a synthetic key (not a raw ML/DailyLog field)
    used only for ``Recommendation.triggered_by`` grouping/display purposes.
* Tier constants reuse ``Recommendation.Priority`` directly (rather than a separate set of
  tier constants) since the rules-table's 5-tier scale ("Low".."High") *is* the priority
  scale for this module now — see recommendations/models.py.
* The AF (age x feed) rules give a Status (Underfed / On Track / Overfed) per combination
  but the source table doesn't assign it a priority tier directly. This file maps
  Underfed -> HIGH (every AF underfeeding rule ties it to a direct yield/growth risk),
  Overfed -> MEDIUM (every AF overfeeding rule frames it as wasted cost, not a yield risk),
  On Track -> LOW. This is the one judgment call in this file that isn't a direct table
  lookup; adjust ``FEED_STATUS_PRIORITY`` if the adviser wants different weighting.
"""

from __future__ import annotations

from dataclasses import dataclass

from recommendations.models import Recommendation

Priority = Recommendation.Priority

# Most-to-least severe, used to resolve the "worse tier wins" merge for temperature vs.
# humidity, and shared by anything that needs to compare two tiers.
SEVERITY_ORDER = [Priority.HIGH, Priority.MEDIUM_HIGH, Priority.MEDIUM, Priority.MEDIUM_LOW, Priority.LOW]

# Raw model/DailyLog features that feed the synthetic "environment" recommendation slot —
# used by importance_for() below, and by forecasting/views.py & dashboard/views.py to sort
# recommendations by RF feature importance without duplicating this list in three places.
ENVIRONMENT_FEATURES = ("temperature_c", "humidity_pct")


def worse_tier(tier_a: str, tier_b: str) -> str:
    """Whichever of two tiers is more severe, per SEVERITY_ORDER."""
    return min(tier_a, tier_b, key=SEVERITY_ORDER.index)


def importance_for(triggered_by: str, feature_importances: dict) -> float:
    """RF feature-importance value to sort a Recommendation by, given its triggered_by key.

    For the synthetic "environment" key (no single raw feature backs it) this is the
    higher of temperature_c/humidity_pct's own importance — the same "most important
    negative factor first" ordering spec section 6 requires, just applied to whichever of
    the two environmental readings the model currently weighs more heavily. Shared by
    recommendations/engine.py (creation order), forecasting/views.py (display grouping),
    and dashboard/views.py (tie-break) so this logic lives in exactly one place.
    """
    if triggered_by == "environment":
        return max(feature_importances.get(f, 0.0) for f in ENVIRONMENT_FEATURES)
    return feature_importances.get(triggered_by, 0.0)


@dataclass(frozen=True)
class FiredRule:
    """A fired recommendation slot, flattened to exactly what a Recommendation row needs."""

    feature: str
    priority: str
    message: str


# --- 1.1 Flock age tiers (Annex A section 1.1) -----------------------------------


def classify_flock_age(age_weeks: float) -> str:
    if age_weeks >= 80:
        return Priority.HIGH  # retire / replacement age
    if age_weeks > 52:
        return Priority.MEDIUM_HIGH  # post-peak decline
    if age_weeks > 26:
        return Priority.MEDIUM  # peak laying
    if age_weeks > 18:
        return Priority.MEDIUM_LOW  # onset of laying
    return Priority.LOW  # pre-lay / immature


# --- 1.2 Temperature tiers (Annex A section 1.2) ----------------------------------


def classify_temperature(temperature_c: float) -> str:
    if temperature_c >= 33:
        return Priority.HIGH  # severe heat stress
    if temperature_c > 27:
        return Priority.MEDIUM_HIGH  # elevated heat stress
    if temperature_c > 23:
        return Priority.MEDIUM  # thermoneutral comfort zone
    if temperature_c > 18:
        return Priority.MEDIUM_LOW  # mild cold stress
    return Priority.LOW  # cold stress risk


# --- 1.3 Humidity tiers (Annex A section 1.3) -------------------------------------


def classify_humidity(humidity_pct: float) -> str:
    if humidity_pct >= 88:
        return Priority.HIGH  # severe moisture stress
    if humidity_pct > 70:
        return Priority.MEDIUM_HIGH  # elevated humidity
    if humidity_pct >= 50:
        return Priority.MEDIUM  # in range
    if humidity_pct > 40:
        return Priority.MEDIUM_LOW  # mild dryness
    return Priority.LOW  # dry conditions


# --- 1.4 Feed intake per bird, tiered by age (Annex A section 1.4) ----------------
# Each age tier has its own (underfed-ceiling, overfed-floor) in grams/bird/day: below the
# first number is underfed, at/above the second is overfed, between is on track.
FEED_TIER_THRESHOLDS_G: dict[str, tuple[float, float]] = {
    Priority.LOW: (70, 120),
    Priority.MEDIUM_LOW: (110, 150),
    Priority.MEDIUM: (115, 160),
    Priority.MEDIUM_HIGH: (110, 150),
    Priority.HIGH: (90, 130),
}

FEED_STATUS_PRIORITY = {"underfed": Priority.HIGH, "on_track": Priority.LOW, "overfed": Priority.MEDIUM}
FEED_STATUS_LABELS = {"underfed": "Underfed", "on_track": "On Track", "overfed": "Overfed"}


def _feed_per_bird_grams(inputs: dict) -> float:
    return (inputs["feed_intake_kg"] / inputs["flock_size"]) * 1000


def classify_feed(age_tier: str, feed_per_bird_g: float) -> str:
    underfed_ceiling, overfed_floor = FEED_TIER_THRESHOLDS_G[age_tier]
    if feed_per_bird_g < underfed_ceiling:
        return "underfed"
    if feed_per_bird_g < overfed_floor:
        return "on_track"
    return "overfed"


# --- 2.1 Flock Age x Feed Intake combination rules (Annex A section 2.1, AF1-AF15) -
# Text is the preventive-recommendation column verbatim; the leading "Increase/Maintain/
# Reduce" phrasing already states the status, so the message wrapper below only needs to
# add the actual readings for traceability.
AF_RULES: dict[tuple[str, str], str] = {
    (Priority.LOW, "underfed"): (
        "Increase toward 70-110 g/bird/day; underfeeding at this stage delays growth and "
        "laying maturity."
    ),
    (Priority.LOW, "on_track"): "Maintain current ration; monitor weekly weight gain.",
    (Priority.LOW, "overfed"): (
        "Reduce ration; excess protein risks leg-development issues in growing ducklings."
    ),
    (Priority.MEDIUM_LOW, "underfed"): (
        "Increase toward 110-130 g/bird/day; risk of delayed/weak start to laying."
    ),
    (Priority.MEDIUM_LOW, "on_track"): "Maintain current ration through the transition into laying.",
    (Priority.MEDIUM_LOW, "overfed"): (
        "Reduce ration; excess feed at this stage has no added production benefit."
    ),
    (Priority.MEDIUM, "underfed"): (
        "Increase toward 115-150 g/bird/day promptly; peak-age underfeeding directly "
        "reduces egg output/size."
    ),
    (Priority.MEDIUM, "on_track"): "Maintain current ration through peak production.",
    (Priority.MEDIUM, "overfed"): "Reduce ration; excess feed at peak is wasted, not converted to yield.",
    (Priority.MEDIUM_HIGH, "underfed"): (
        "Increase toward 110-140 g/bird/day to avoid unnecessary output loss."
    ),
    (Priority.MEDIUM_HIGH, "on_track"): "Maintain current, appropriately reduced ration.",
    (Priority.MEDIUM_HIGH, "overfed"): (
        "Reduce ration; feed cost return is falling as production declines."
    ),
    (Priority.HIGH, "underfed"): (
        "Increase toward 90-120 g/bird/day; even spent layers need a maintenance minimum."
    ),
    (Priority.HIGH, "on_track"): "Maintain current maintenance-level ration.",
    (Priority.HIGH, "overfed"): (
        "Reduce ration; consider evaluating flock replacement given minimal remaining output."
    ),
}


# --- 2.6 Flock age standalone text (Annex A section 2.6) -------------------------
FLOCK_AGE_TEXT: dict[str, str] = {
    Priority.HIGH: (
        "Retire or cull declining layers and shift fully to the replacement cohort. Holding "
        "spent birds past this point mostly adds feed cost with little egg return."
    ),
    Priority.MEDIUM_HIGH: (
        "Start raising a replacement cohort now — this is the actionable preventive window. "
        "New layers typically need 22-26 weeks from hatching to first lay depending on strain "
        "and management conditions, so initiate orders and brooding now to avoid a production gap."
    ),
    Priority.MEDIUM: (
        "No replacement action needed; focus on protecting production during this "
        "peak/sustained laying window."
    ),
    Priority.MEDIUM_LOW: (
        "Confirm laying has started on schedule (~22-23 weeks); a noticeable delay may "
        "signal a nutrition or health issue."
    ),
    Priority.LOW: (
        "No retirement/replacement action relevant yet — flock is still growing and not "
        "expected to lay."
    ),
}


# --- 2.2 / 2.3 Temperature <-> humidity flag matrices (Annex A) -------------------
# Both are keyed (row_tier, column_tier) exactly as the source table lays them out — they
# are NOT the same matrix read two ways: looking up (temperature, humidity) for the
# "temperature flag" gives a different result than (humidity, temperature) for the
# "humidity flag" in several cells, so each needs its own table.
TEMPERATURE_FLAG_MATRIX: dict[tuple[str, str], str] = {
    (Priority.HIGH, Priority.HIGH): Priority.HIGH,
    (Priority.HIGH, Priority.MEDIUM_HIGH): Priority.HIGH,
    (Priority.HIGH, Priority.MEDIUM): Priority.HIGH,
    (Priority.HIGH, Priority.MEDIUM_LOW): Priority.MEDIUM_HIGH,
    (Priority.HIGH, Priority.LOW): Priority.MEDIUM_HIGH,
    (Priority.MEDIUM_HIGH, Priority.HIGH): Priority.HIGH,
    (Priority.MEDIUM_HIGH, Priority.MEDIUM_HIGH): Priority.MEDIUM_HIGH,
    (Priority.MEDIUM_HIGH, Priority.MEDIUM): Priority.MEDIUM_HIGH,
    (Priority.MEDIUM_HIGH, Priority.MEDIUM_LOW): Priority.MEDIUM_HIGH,
    (Priority.MEDIUM_HIGH, Priority.LOW): Priority.MEDIUM,
    (Priority.MEDIUM, Priority.HIGH): Priority.MEDIUM_HIGH,
    (Priority.MEDIUM, Priority.MEDIUM_HIGH): Priority.MEDIUM,
    (Priority.MEDIUM, Priority.MEDIUM): Priority.MEDIUM,
    (Priority.MEDIUM, Priority.MEDIUM_LOW): Priority.MEDIUM,
    (Priority.MEDIUM, Priority.LOW): Priority.MEDIUM_LOW,
    (Priority.MEDIUM_LOW, Priority.HIGH): Priority.MEDIUM_HIGH,
    (Priority.MEDIUM_LOW, Priority.MEDIUM_HIGH): Priority.MEDIUM_LOW,
    (Priority.MEDIUM_LOW, Priority.MEDIUM): Priority.MEDIUM_LOW,
    (Priority.MEDIUM_LOW, Priority.MEDIUM_LOW): Priority.MEDIUM_LOW,
    (Priority.MEDIUM_LOW, Priority.LOW): Priority.MEDIUM_LOW,
    (Priority.LOW, Priority.HIGH): Priority.MEDIUM_HIGH,
    (Priority.LOW, Priority.MEDIUM_HIGH): Priority.MEDIUM_LOW,
    (Priority.LOW, Priority.MEDIUM): Priority.LOW,
    (Priority.LOW, Priority.MEDIUM_LOW): Priority.LOW,
    (Priority.LOW, Priority.LOW): Priority.LOW,
}

HUMIDITY_FLAG_MATRIX: dict[tuple[str, str], str] = {
    (Priority.HIGH, Priority.HIGH): Priority.HIGH,
    (Priority.HIGH, Priority.MEDIUM_HIGH): Priority.HIGH,
    (Priority.HIGH, Priority.MEDIUM): Priority.HIGH,
    (Priority.HIGH, Priority.MEDIUM_LOW): Priority.HIGH,
    (Priority.HIGH, Priority.LOW): Priority.HIGH,
    (Priority.MEDIUM_HIGH, Priority.HIGH): Priority.HIGH,
    (Priority.MEDIUM_HIGH, Priority.MEDIUM_HIGH): Priority.HIGH,
    (Priority.MEDIUM_HIGH, Priority.MEDIUM): Priority.MEDIUM_HIGH,
    (Priority.MEDIUM_HIGH, Priority.MEDIUM_LOW): Priority.MEDIUM_HIGH,
    (Priority.MEDIUM_HIGH, Priority.LOW): Priority.MEDIUM_HIGH,
    (Priority.MEDIUM, Priority.HIGH): Priority.MEDIUM,
    (Priority.MEDIUM, Priority.MEDIUM_HIGH): Priority.MEDIUM,
    (Priority.MEDIUM, Priority.MEDIUM): Priority.MEDIUM,
    (Priority.MEDIUM, Priority.MEDIUM_LOW): Priority.MEDIUM,
    (Priority.MEDIUM, Priority.LOW): Priority.MEDIUM,
    (Priority.MEDIUM_LOW, Priority.HIGH): Priority.MEDIUM,
    (Priority.MEDIUM_LOW, Priority.MEDIUM_HIGH): Priority.MEDIUM,
    (Priority.MEDIUM_LOW, Priority.MEDIUM): Priority.MEDIUM_LOW,
    (Priority.MEDIUM_LOW, Priority.MEDIUM_LOW): Priority.MEDIUM_LOW,
    (Priority.MEDIUM_LOW, Priority.LOW): Priority.MEDIUM_LOW,
    (Priority.LOW, Priority.HIGH): Priority.MEDIUM_LOW,
    (Priority.LOW, Priority.MEDIUM_HIGH): Priority.MEDIUM_LOW,
    (Priority.LOW, Priority.MEDIUM): Priority.LOW,
    (Priority.LOW, Priority.MEDIUM_LOW): Priority.LOW,
    (Priority.LOW, Priority.LOW): Priority.LOW,
}


# --- 2.4 / 2.5 Recommendation text by resultant flag tier ---------------------------------
TEMPERATURE_FLAG_TEXT: dict[str, str] = {
    Priority.HIGH: (
        "Severe heat stress risk. Increase ventilation/fans immediately, ensure shade and "
        "cool water/bathing access, consider roof misting. Expect reduced feed intake and a "
        "likely drop in egg yield forecast."
    ),
    Priority.MEDIUM_HIGH: (
        "Elevated heat stress. Improve airflow, check shade/water access adequate for flock "
        "size, monitor feed intake."
    ),
    Priority.MEDIUM: "Within acceptable comfort range. Maintain current housing ventilation and shade setup.",
    Priority.MEDIUM_LOW: (
        "Mild cold stress risk. Reduce drafts, check windbreaks; ducklings may need "
        "supplemental warmth."
    ),
    Priority.LOW: (
        "Cold stress risk. Provide draft-free, insulated housing and supplemental heat, "
        "especially for ducklings."
    ),
}

HUMIDITY_FLAG_TEXT: dict[str, str] = {
    Priority.HIGH: (
        "Severe moisture stress. Increase ventilation urgently, clear wet litter, check "
        "drainage, consider reducing stocking density."
    ),
    Priority.MEDIUM_HIGH: "Elevated humidity. Improve ventilation, inspect litter condition, watch for respiratory signs.",
    Priority.MEDIUM: "Within acceptable range. Maintain current housing ventilation.",
    Priority.MEDIUM_LOW: "Slightly dry, generally not harmful. Ensure water access, especially if paired with heat.",
    Priority.LOW: "Very dry. Monitor water intake if paired with heat; lower priority on its own.",
}


def evaluate_rules(inputs: dict, feature_importances: dict) -> list[FiredRule]:
    """Forward-chain over the 3 recommendation slots, ordered by RF feature importance.

    ``feature_importances`` is the Forecast's raw ``{feature_name: importance}`` mapping
    (not pre-sorted) — ``importance_for`` handles both raw-feature and synthetic
    ("environment") keys, so the 3 fired slots can always be sorted the same way regardless
    of which underlying feature(s) they represent.
    """
    age_tier = classify_flock_age(inputs["flock_age_weeks"])
    temp_tier = classify_temperature(inputs["temperature_c"])
    humidity_tier = classify_humidity(inputs["humidity_pct"])
    feed_per_bird_g = _feed_per_bird_grams(inputs)
    feed_status = classify_feed(age_tier, feed_per_bird_g)

    feed_message = (
        f"Flock age {inputs['flock_age_weeks']} weeks, feed intake {feed_per_bird_g:.0f} "
        f"g/bird/day ({FEED_STATUS_LABELS[feed_status]}). {AF_RULES[(age_tier, feed_status)]}"
    )
    age_message = (
        f"Flock age is {inputs['flock_age_weeks']} weeks ({age_tier.label}). "
        f"{FLOCK_AGE_TEXT[age_tier]}"
    )

    temp_flag = TEMPERATURE_FLAG_MATRIX[(temp_tier, humidity_tier)]
    humidity_flag = HUMIDITY_FLAG_MATRIX[(humidity_tier, temp_tier)]
    env_priority = worse_tier(temp_flag, humidity_flag)
    env_message = (
        f"Temperature {inputs['temperature_c']:.1f}°C, Humidity {inputs['humidity_pct']:.0f}% "
        f"— Environmental status: {env_priority.label}. {TEMPERATURE_FLAG_TEXT[temp_flag]} "
        f"{HUMIDITY_FLAG_TEXT[humidity_flag]}"
    )

    fired = [
        FiredRule(feature="feed_intake_kg", priority=FEED_STATUS_PRIORITY[feed_status], message=feed_message),
        FiredRule(feature="flock_age_weeks", priority=age_tier, message=age_message),
        FiredRule(feature="environment", priority=env_priority, message=env_message),
    ]
    fired.sort(key=lambda f: importance_for(f.feature, feature_importances), reverse=True)
    return fired
