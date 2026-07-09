"""Prescriptive analytics rule engine orchestration (itikcare-spec.md section 6).

Forward chaining (IF-THEN rules) combined with the RF model's feature importance
scores. Every recommendation is traceable back to which variable/rule triggered
it — no black-box output. The rule definitions and evaluation logic live in
``recommendations/rules.py`` (kept DB-free and unit-testable); this module is the
thin layer that pulls a Forecast's data from the DB and persists the result, the
same split as ``forecasting/pipeline.py`` vs. the ``train_forecast_model`` command.
"""

from django.db import transaction

from forecasting.models import Forecast

from . import rules
from .models import Recommendation


def generate_recommendations(forecast: Forecast) -> list[Recommendation]:
    """Evaluate the rule engine for a single Forecast and (re)create its Recommendation rows.

    "Current farm inputs" (spec section 6) means the most recently logged DailyLog
    among the Forecast's own source_logs — the freshest real reading available at
    forecast time. Regeneration is idempotent: any Recommendations already attached
    to this forecast are replaced, so re-running (e.g. after a model retrain) never
    duplicates rows.
    """
    current_log = forecast.source_logs.order_by("-date").first()
    if current_log is None:
        return []

    inputs = {
        "flock_age_weeks": current_log.flock_age_weeks,
        "feed_intake_kg": float(current_log.feed_intake_kg),
        "flock_size": current_log.flock_size,
        "temperature_c": float(current_log.temperature_c),
        "humidity_pct": float(current_log.humidity_pct),
    }
    importance_order = sorted(
        forecast.feature_importances, key=forecast.feature_importances.get, reverse=True
    )
    fired = rules.evaluate_rules(inputs, importance_order)

    with transaction.atomic():
        forecast.recommendations.all().delete()
        created = Recommendation.objects.bulk_create(
            [
                Recommendation(
                    forecast=forecast,
                    triggered_by=f.feature,
                    message=f.message,
                    priority=f.priority,
                )
                for f in fired
            ]
        )
    return created
