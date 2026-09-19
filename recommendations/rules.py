"""Pure forward-chaining rule logic for the prescriptive module (itikcare-spec.md section 6).

Kept free of Django/ORM imports — like ``forecasting/pipeline.py`` — so the rules can be
unit-tested as plain functions over dicts. ``recommendations/engine.py`` is the thin
orchestration layer that pulls a Forecast's data from the DB, calls into here, and persists
Recommendation rows.

Threshold source (see CLAUDE.md's "no black box" requirement):

* Every tier boundary, combination table, and message below is copied verbatim from the
  adviser-provided ``itikcare_rules-table-WITH-LEARN-MORE.docx`` ("ItikCare Prescriptive
  Recommendations — Rules/Threshold Table") — this is the single source of truth for the
  prescriptive module, superseding the earlier ``Annex-A-Rules-Table.pdf`` draft it was
  built from (kept in the repo for provenance; its tier boundaries and combination tables
  are unchanged here — only the action/rationale wording was reworded to be plainer and
  more farmer-friendly). Anything not in that table (e.g. a cold-stress or dry-humidity
  tier this farm's own historical CSV never happened to record) is still implemented,
  because the table is the spec regardless of what one farm's data happened to show.
* Recommendations collapse to exactly **4 slots**, always fired (nothing silently skipped,
  same forward-chaining philosophy as before) — see ``evaluate_rules``:
  - ``feed_intake_kg`` — flock age tier x age-tiered feed tier -> one of the AF1-AF15
    combination rules (the rules-table's 2.1). Feed thresholds are themselves age-dependent
    (1.4), so age and feed are evaluated together, not independently.
  - ``flock_age_weeks`` — flock age tier alone -> retirement/replacement guidance
    (the rules-table's 2.6). A separate concern from the feed-ration advice above (this is
    about flock succession planning, not day-to-day feeding).
  - ``temperature_c`` — temperature's 5-tier reading combined with humidity's via the
    asymmetric "temperature flag" matrix (the rules-table's 2.2), producing its own resultant tier,
    status, and recommendation text (2.4).
  - ``humidity_pct`` — humidity's 5-tier reading combined with temperature's via the
    asymmetric "humidity flag" matrix (the rules-table's 2.3), producing its own resultant tier,
    status, and recommendation text (2.5).
  Temperature and humidity used to be merged into one synthetic "environment" slot
  ("worse tier wins", both texts concatenated into one message). They're now independent
  Recommendation rows: the rules-table itself treats 2.4 and 2.5 as separate tables with their own
  Preventive Recommendation/Learn More text, each matrix lookup still depends on *both*
  readings (that coupling isn't lost), but flattening the two results into one message
  hid which variable actually drove which piece of advice — this split restores that
  per-variable traceability (CLAUDE.md's "no black box" requirement) and lets the two
  render as separate, individually-traceable cards in the UI.
* Tier constants reuse ``Recommendation.Priority`` directly (rather than a separate set of
  tier constants) since the rules-table's 5-tier scale ("Low".."High") *is* the priority
  scale for this module now — see recommendations/models.py.
* The AF (age x feed) rules give a Status (Underfed / On Track / Overfed) per combination
  but the source table doesn't assign it a priority tier directly. This file maps
  Underfed -> HIGH (every AF underfeeding rule ties it to a direct yield/growth risk),
  Overfed -> MEDIUM (every AF overfeeding rule frames it as wasted cost, not a yield risk),
  On Track -> LOW. This is the one judgment call in this file that isn't a direct table
  lookup; adjust ``FEED_STATUS_PRIORITY`` if the adviser wants different weighting.
* Every fired rule carries a short ``status`` label (shown before its action text in the
  UI, e.g. "UNDERFED - Give more feed. Aim for..."), a ``reading_summary`` (the raw number
  that triggered it, for traceability), and a ``learn_more`` rationale paragraph, in
  addition to the actionable ``action_text``. None of these are invented: ``status`` is
  always either the rules-table's "Status" column value (feed) or the tier's own label (age/
  temperature/humidity — the rules-table's "Tier"/"Result" column for these is the tier itself),
  and ``learn_more`` is transcribed verbatim from the rules-table's "Learn More" column.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from recommendations.models import Recommendation

Priority = Recommendation.Priority


class RuleText(NamedTuple):
    """One rules-table cell: the actionable advice plus its rationale."""

    action_text: str
    learn_more: str


def importance_for(triggered_by: str, feature_importances: dict) -> float:
    """RF feature-importance value to sort a Recommendation by, given its triggered_by key.

    Now a plain lookup: every triggered_by value (feed_intake_kg, flock_age_weeks,
    temperature_c, humidity_pct) is itself a raw feature name present in
    feature_importances, so there's no synthetic-key special case to handle. Shared by
    recommendations/engine.py (creation order), forecasting/views.py (display grouping),
    and dashboard/views.py (tie-break) so this logic lives in exactly one place.
    """
    return feature_importances.get(triggered_by, 0.0)


@dataclass(frozen=True)
class FiredRule:
    """A fired recommendation slot, flattened to exactly what a Recommendation row needs."""

    feature: str
    priority: str
    status: str
    action_text: str
    learn_more: str
    reading_summary: str


# --- 1.1 Flock age tiers (the rules-table's section 1.1) -----------------------------------


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


# --- 1.2 Temperature tiers (the rules-table's section 1.2) ----------------------------------


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


# --- 1.3 Humidity tiers (the rules-table's section 1.3) -------------------------------------


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


# --- 1.4 Feed intake per bird, tiered by age (the rules-table's section 1.4) ----------------
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


# --- 2.1 Flock Age x Feed Intake combination rules (the rules-table's section 2.1, AF1-AF15) -
AF_RULES: dict[tuple[str, str], RuleText] = {
    (Priority.LOW, "underfed"): RuleText(
        "Give more feed. Aim for 70–110 grams per bird every day. If not enough feed, "
        "the ducks will grow slow and lay eggs late.",
        "Baby ducks need good food every day to grow big. If they don't get enough food "
        "for a long time, they will lay eggs late. Note: ducklings from week 1 to 5 eat "
        "less than 70 g a day, and that's normal — don't mark them as underfed.",
    ),
    (Priority.LOW, "on_track"): RuleText(
        "Keep giving the same amount of feed. Check their weight every week.",
        "Weighing some birds every week is the easiest way to check if the ducklings are "
        "growing well. Even if the feed amount looks right on paper, it can still fall "
        "short if there's not enough feeder space, not enough water, or too many ducks "
        "fighting for food — so keep checking weight often. Note: ducklings from week 1 "
        "to 5 will naturally eat less than 70 g a day and that's still normal. Just watch "
        "if their weight is going up.",
    ),
    (Priority.LOW, "overfed"): RuleText(
        "Give less feed. Too much feed can cause leg problems in growing ducklings.",
        "When young ducks grow too fast because of too much feed, they can get leg "
        "problems like bent or twisted legs, or “angel wing,” where the wing feathers "
        "twist outward. Both problems come from too much food. Give the right amount of "
        "feed, not rich or high-protein feed.",
    ),
    (Priority.MEDIUM_LOW, "underfed"): RuleText(
        "Give more feed, about 110–130 grams per bird per day. If not, egg-laying may "
        "start late or weak.",
        "Around week 18 to 26, the ducks' bodies are getting ready to lay eggs. They need "
        "enough food now. If not enough, you will see the problem later — laying starts "
        "late or not steady.",
    ),
    (Priority.MEDIUM_LOW, "on_track"): RuleText(
        "Keep the same feed while they are getting ready to lay eggs.",
        "This is before the first egg, around week 22–23. Keep the feed the same. Do "
        "not change the feed right before laying starts — this can cause stress and "
        "delay the eggs.",
    ),
    (Priority.MEDIUM_LOW, "overfed"): RuleText(
        "Give less feed. Extra feed now does not help them lay more eggs.",
        "Unlike the growing stage, too much feed here won't hurt the legs or wings, but "
        "it also won't make ducks start laying sooner or better. The extra feed is "
        "basically wasted money.",
    ),
    (Priority.MEDIUM, "underfed"): RuleText(
        "Give more feed right away, about 115–150 grams per bird per day. Not enough "
        "feed now means fewer and smaller eggs.",
        "This is peak laying time. Ducks need the most food now because they lay eggs "
        "almost every day. Even a short time of not enough feed at peak can quickly mean "
        "fewer and smaller eggs, because they don't have much stored energy to use.",
    ),
    (Priority.MEDIUM, "on_track"): RuleText(
        "Keep the same feed during peak egg-laying time.",
        "This is when the flock lays the most eggs, usually week 26 to 52. Keep the feed "
        "steady. This protects the number and size of the eggs.",
    ),
    (Priority.MEDIUM, "overfed"): RuleText(
        "Give less feed. Extra feed at peak doesn't turn into more eggs — it's just "
        "wasted.",
        "Laying ducks need a fixed amount of food each day. More food than that will not "
        "give more eggs. It just costs more money and can make ducks too fat, which can "
        "lower egg production later.",
    ),
    (Priority.MEDIUM_HIGH, "underfed"): RuleText(
        "Give more feed, about 110–140 grams per bird per day, so you don't lose more "
        "eggs than needed.",
        "Egg number goes down naturally after peak time. But not enough feed makes it go "
        "down even faster. Giving the right feed now protects the flock's remaining "
        "productive days.",
    ),
    (Priority.MEDIUM_HIGH, "on_track"): RuleText(
        "Keep the feed the same, adjusted properly for this after-peak stage.",
        "After peak laying, egg number slowly goes down and ducks need a little less "
        "feed too. A slightly lower feed amount at this stage is correct. It is not a "
        "bad thing.",
    ),
    (Priority.MEDIUM_HIGH, "overfed"): RuleText(
        "Give less feed. Production is going down, so you get less value for the feed "
        "cost.",
        "As flocks pass peak, the eggs you get per kilo of feed get worse. Giving too "
        "much feed to a flock that is slowing down is a big waste of money.",
    ),
    (Priority.HIGH, "underfed"): RuleText(
        "Give more feed, about 90–120 grams per bird per day. Even old layers need a "
        "minimum amount to stay healthy.",
        "Old layers (around week 72–80 and up) mostly eat just to stay healthy, not to "
        "make eggs. DOST-PCAARRD recommends culling some birds every year to keep the "
        "flock efficient, so even older birds still need this minimum feed. Skipping it "
        "can cause too much weight loss, weak immunity, and welfare problems.",
    ),
    (Priority.HIGH, "on_track"): RuleText(
        "Keep the same maintenance-level feed.",
        "At this stage, feed is mainly to keep the ducks healthy, not to make eggs. Keep "
        "a steady feed amount until the flock is culled or replaced.",
    ),
    (Priority.HIGH, "overfed"): RuleText(
        "Give less feed, and think about replacing the flock since they're producing "
        "very little now.",
        "Once a flock reaches retirement age (around week 72–80), egg output has gone "
        "down a lot. Feeding them more now mostly wastes money. This is the time to plan "
        "culling and get new layers.",
    ),
}


# --- 2.6 Flock age standalone text (the rules-table's section 2.6) -------------------------
FLOCK_AGE_TEXT: dict[str, RuleText] = {
    Priority.HIGH: RuleText(
        "Retire or cull the layers that are slowing down, and move fully to the "
        "replacement ducks. Keeping old birds past this point mostly adds feed cost "
        "without much egg return.",
        "Layers are usually productive until about week 72–80. After that, egg output "
        "drops so much that feeding them costs more than the eggs they give. Culling on "
        "time protects your farm's money.",
    ),
    Priority.MEDIUM_HIGH: RuleText(
        "Start raising your replacement ducks now. New layers need about 22–26 weeks "
        "from hatching before they start laying. So order and start brooding now, so "
        "you don't have a gap with no eggs.",
        "The 22–26 weeks has two parts. About 22–23 weeks is how long a duckling needs "
        "to grow before it can lay (a little different per breed — IP-Kayumanggi around "
        "week 20, IP-Khaki around week 22, IP-Itim around week 23). The rest is extra "
        "time for getting the ducks from the hatchery to your farm — buying, transport, "
        "and settling in — which can add 2–4 more weeks. If your supplier has been slow "
        "before, order earlier, closer to the longer end.",
    ),
    Priority.MEDIUM: RuleText(
        "No need to plan replacements yet. Focus on protecting production during this "
        "peak/sustained laying period.",
        "This is the flock's most valuable production window. Focus on good feeding, "
        "comfortable housing, and cleanliness now, since problems now have the biggest "
        "effect on total egg output for the whole laying cycle.",
    ),
    Priority.MEDIUM_LOW: RuleText(
        "Check if your ducks started laying on time (around week 22–23). If they are "
        "clearly late, check their recent feeding and health.",
        "Starting to lay on time is a good health sign. A flock that starts late often "
        "had a feeding problem or health problem earlier while growing. If they're "
        "late, check your records — it's not always something to worry a lot about.",
    ),
    Priority.LOW: RuleText(
        "No need for retirement or replacement action yet. The flock is still growing "
        "and not ready to lay eggs.",
        "Ducklings need to finish growing first before their body can make eggs. At "
        "this stage, focus on feeding and growth, not on planning egg production.",
    ),
}


# --- 2.2 / 2.3 Temperature <-> humidity flag matrices -------------------
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
TEMPERATURE_FLAG_TEXT: dict[str, RuleText] = {
    Priority.HIGH: RuleText(
        "Danger — very high risk of heat stress. Turn on more fans right away. Make "
        "sure there is shade and cool water for drinking and bathing. Think about "
        "spraying water on the roof. The ducks will eat less and lay fewer eggs until "
        "it gets cooler.",
        "Ducks cool down mostly by panting and using water, since they can't sweat. "
        "Studies on laying ducks show that once heat stress starts, feed intake, egg "
        "number, and egg size all drop within days, and can stay low even after the "
        "temperature goes back to normal.",
    ),
    Priority.MEDIUM_HIGH: RuleText(
        "Warning — heat stress is starting to rise. Improve airflow, make sure shade "
        "and water are enough for the flock size, and watch how much they're eating.",
        "At this level, heat is already quietly hurting the ducks' appetite and "
        "comfort, even before you see heavy panting. Fixing it early with better "
        "airflow and water can stop it from becoming worse.",
    ),
    Priority.MEDIUM: RuleText(
        "This is a comfortable temperature. Keep the current ventilation and shade the "
        "same.",
        "This is usually the comfortable temperature for laying ducks. They don't need "
        "extra energy to stay warm or cool. Egg production is most stable in this "
        "range.",
    ),
    Priority.MEDIUM_LOW: RuleText(
        "Mild risk of cold stress. Reduce drafts (wind coming through gaps). Check "
        "windbreaks. Ducklings may need extra heat.",
        "Young ducklings lose body heat faster than adult ducks. A small drop in "
        "temperature that adult ducks are fine with can already be dangerous for baby "
        "ducks.",
    ),
    Priority.LOW: RuleText(
        "Risk of cold stress. Give housing with no drafts (wind coming through gaps), "
        "good insulation, and extra heat — especially for ducklings.",
        "Cold makes ducks use more energy just to stay warm. That's why they eat more "
        "in cold weather, even while eggs become fewer and lower quality. Ducklings "
        "are at higher risk than adults because they can't regulate their own body "
        "temperature well yet.",
    ),
}

HUMIDITY_FLAG_TEXT: dict[str, RuleText] = {
    Priority.HIGH: RuleText(
        "Danger — too much moisture. Improve ventilation right away. Clean out wet "
        "litter (bedding), check the drainage, and think about lowering the number of "
        "birds per space.",
        "Wet litter turns into ammonia gas, which irritates ducks' airways and makes "
        "them more likely to get respiratory infections. Wet, humid places also let "
        "bad bacteria and mold grow faster. Act fast to protect the flock's health.",
    ),
    Priority.MEDIUM_HIGH: RuleText(
        "Humidity is getting high. Improve ventilation, check the litter condition, "
        "and watch for signs of breathing trouble.",
        "Litter that stays too wet for too long starts making ammonia before you can "
        "even smell it. Check the litter (bedding) often so you can fix wet spots "
        "early, before it becomes a big breathing problem for the whole flock. Watch "
        "your ducks for coughing, sneezing, or hard breathing.",
    ),
    Priority.MEDIUM: RuleText(
        "This is an acceptable range. Keep the current housing ventilation.",
        "This range keeps litter (bedding) dry enough to limit ammonia buildup, but "
        "not too dry to cause dust and dehydration problems. Most of the research "
        "behind this comes from closed broiler houses; semi-open duck housing common "
        "in the Philippines usually gets more natural airflow and outdoor access, so "
        "it may handle the higher end of this range a bit better. Either way, if you "
        "smell ammonia or see uncomfortable birds, improve ventilation no matter what "
        "type of housing you have.",
    ),
    Priority.MEDIUM_LOW: RuleText(
        "A bit dry, but usually not harmful. Just make sure water is always "
        "available, especially if it's also hot.",
        "A little dryness only becomes a problem when combined with heat, because dry "
        "air makes ducks lose body water faster from panting. On its own, at normal "
        "temperatures, this level of dryness has little effect on the flock.",
    ),
    Priority.LOW: RuleText(
        "Very dry. Watch their water intake if it's also hot.",
        "Very dry air causes more dust and the ducks need a bit more water. But ducks "
        "are usually fine with this unless it's also very hot. Just make sure water is "
        "always there.",
    ),
}


def evaluate_rules(inputs: dict, feature_importances: dict) -> list[FiredRule]:
    """Forward-chain over the 4 recommendation slots, ordered by RF feature importance.

    ``feature_importances`` is the Forecast's raw ``{feature_name: importance}`` mapping
    (not pre-sorted) — since every triggered_by value below is itself a raw feature name,
    ``importance_for`` is a plain lookup for all 4 slots.
    """
    age_tier = classify_flock_age(inputs["flock_age_weeks"])
    temp_tier = classify_temperature(inputs["temperature_c"])
    humidity_tier = classify_humidity(inputs["humidity_pct"])
    feed_per_bird_g = _feed_per_bird_grams(inputs)
    feed_status = classify_feed(age_tier, feed_per_bird_g)

    feed_rule = AF_RULES[(age_tier, feed_status)]
    age_rule = FLOCK_AGE_TEXT[age_tier]
    temp_flag = TEMPERATURE_FLAG_MATRIX[(temp_tier, humidity_tier)]
    humidity_flag = HUMIDITY_FLAG_MATRIX[(humidity_tier, temp_tier)]
    temp_rule = TEMPERATURE_FLAG_TEXT[temp_flag]
    humidity_rule = HUMIDITY_FLAG_TEXT[humidity_flag]

    fired = [
        FiredRule(
            feature="feed_intake_kg",
            priority=FEED_STATUS_PRIORITY[feed_status],
            status=FEED_STATUS_LABELS[feed_status],
            action_text=feed_rule.action_text,
            learn_more=feed_rule.learn_more,
            reading_summary=f"{feed_per_bird_g:.0f} g/bird/day (flock age {inputs['flock_age_weeks']} wk)",
        ),
        FiredRule(
            feature="flock_age_weeks",
            priority=age_tier,
            status=age_tier.label,
            action_text=age_rule.action_text,
            learn_more=age_rule.learn_more,
            reading_summary=f"{inputs['flock_age_weeks']} weeks",
        ),
        FiredRule(
            feature="temperature_c",
            priority=temp_flag,
            status=temp_flag.label,
            action_text=temp_rule.action_text,
            learn_more=temp_rule.learn_more,
            reading_summary=f"{inputs['temperature_c']:.1f}°C",
        ),
        FiredRule(
            feature="humidity_pct",
            priority=humidity_flag,
            status=humidity_flag.label,
            action_text=humidity_rule.action_text,
            learn_more=humidity_rule.learn_more,
            reading_summary=f"{inputs['humidity_pct']:.0f}%",
        ),
    ]
    fired.sort(key=lambda f: importance_for(f.feature, feature_importances), reverse=True)
    return fired
