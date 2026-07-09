"""Repeatable Random Forest training/retraining pipeline (scaffold only).

Run as: python manage.py train_forecast_model

Per CLAUDE.md, retraining must be a repeatable command (not a one-off notebook) so it
can run periodically as new DailyLog data comes in. This file is a structural
placeholder — the actual pipeline is future work. Planned stages, in order:

1. Load DailyLog rows via the ORM, ordered by flock and date.
2. Segment into contiguous runs using Flock (generation boundary) and any date gaps
   within a flock's own logs (a caging-period boundary, per itikcare-spec.md section
   10). Lag/rolling features (e.g. "yesterday's yield") must never be built across a
   segment boundary — the days on either side aren't operationally connected.
3. Feature engineering: flock_size, flock_age_weeks, feed_intake_kg, temperature_c,
   humidity_pct, plus any within-segment lag/rolling features. Do NOT feed
   Caging_Period/Flock_Generation-style boundary markers themselves in as raw
   features — they would let the model memorize specific time periods instead of
   learning general patterns.
4. 80:20 train/test split (chronological within segments, not random shuffle, to
   avoid leaking future data into training).
5. Train a RandomForestRegressor (scikit-learn) predicting daily egg yield; derive
   tri-day yield from consecutive daily predictions.
6. Validate against itikcare-spec.md section 5 acceptance thresholds: MAE <= 8% of
   average daily yield, RMSE <= 10%, MAPE <= 15%, R^2 >= 0.75. Refuse to persist a
   model artifact that fails these thresholds.
7. Persist the trained model artifact (e.g. joblib) plus its feature importances, so
   Forecast.feature_importances can be populated at prediction time and the
   prescriptive module can prioritize rules by them.
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Train or retrain the Random Forest egg yield forecasting model."

    def handle(self, *args, **options):
        raise CommandError(
            "train_forecast_model is not yet implemented — see this file's module "
            "docstring for the planned pipeline stages."
        )
