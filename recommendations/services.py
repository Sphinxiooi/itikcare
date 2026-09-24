"""Picks which recommendation on a Forecast is worth interrupting the farmer for.

Shared by the dashboard's Quick Recommendation card (dashboard/views.py) and the
header notification bell (notifications/services.py) so both surfaces always agree on
what counts as "urgent" -- neither can drift to a different priority cutoff.
"""

from recommendations import rules as recommendation_rules
from recommendations.models import Recommendation

# Lower rank = shown first. Independent of feature-importance order -- the dashboard's
# single "Quick Recommendation" card is about urgency (what needs attention right now),
# not which feature the model currently weighs most; the full importance-ordered list
# lives on the Forecast & Recommendations page (forecasting/views.py).
PRIORITY_RANK = {
    Recommendation.Priority.HIGH: 0,
    Recommendation.Priority.MEDIUM_HIGH: 1,
    Recommendation.Priority.MEDIUM: 2,
    Recommendation.Priority.MEDIUM_LOW: 3,
    Recommendation.Priority.LOW: 4,
}

# Tiers the rules table itself treats as elevated/at-risk (rules-table.pdf sections
# 2.2-2.6) -- only these interrupt the farmer (dashboard card, notification bell).
# Everything milder (on-track/in-range/no-action confirmations) still exists and is
# traceable on the full Forecast & Recommendations page, just not surfaced there.
ACTIONABLE_PRIORITIES = {Recommendation.Priority.HIGH, Recommendation.Priority.MEDIUM_HIGH}


def most_urgent_recommendation(forecast):
    """The forecast's single most urgent Recommendation, or None if it has none.

    Highest priority wins; ties are broken by the triggering feature's RF importance
    (matching the ordering convention on the Forecast & Recommendations page). The
    result may still be a calm, non-actionable tier -- callers check it against
    ACTIONABLE_PRIORITIES themselves, since the dashboard distinguishes "nothing urgent"
    from "no recommendations at all".
    """
    if forecast is None:
        return None
    all_recs = list(forecast.recommendations.all())
    if not all_recs:
        return None
    return min(
        all_recs,
        key=lambda r: (
            PRIORITY_RANK.get(r.priority, 99),
            -recommendation_rules.importance_for(r.triggered_by, forecast.feature_importances),
        ),
    )
