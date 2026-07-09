"""Prescriptive analytics rule engine (scaffold only).

Per itikcare-spec.md section 6 and CLAUDE.md: forward chaining (IF-THEN rules)
combined with the RF model's feature importance scores. Every recommendation must
be traceable back to which variable/rule triggered it — no black-box output.

Planned design (not yet implemented):

- A rule is a plain (condition, message, priority) tuple/dataclass keyed by feature
  name, e.g. a temperature rule fires when Forecast.feature_importances shows
  temperature_c as the dominant negative factor AND the current DailyLog reading
  crosses a heat-stress threshold.
- generate_recommendations(forecast) reads forecast.feature_importances, sorts
  features by importance, and evaluates each feature's rule(s) in that priority
  order — so the highest-importance negative factor is flagged first (e.g. heat
  stress before a minor feed-intake dip), matching spec section 6's prioritization
  requirement.
- Each fired rule creates one Recommendation row with triggered_by set to the
  feature name that fired it, so the dashboard can always show "why" a
  recommendation appeared.
- Acceptance thresholds this module must eventually be validated against (spec
  section 6): Concordance Rate >= 80%, Prescriptive Effectiveness Rate >= 75%,
  False Recommendation Rate <= 10%.
"""

from forecasting.models import Forecast


def generate_recommendations(forecast: Forecast):
    """Evaluate the rule engine for a single Forecast and create Recommendation rows.

    Not yet implemented — see this module's docstring for the planned design.
    """

    raise NotImplementedError(
        "generate_recommendations is not yet implemented — see recommendations/engine.py "
        "module docstring for the planned forward-chaining design."
    )
