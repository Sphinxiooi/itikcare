"""Inference orchestration: turns a freshly-saved DailyLog into a Forecast (+ downstream
Recommendations).

Mirrors the "pure logic vs. thin DB orchestration" split already used elsewhere in this
project: modelling logic lives in ``pipeline.py`` (DB-agnostic), this module is the
ORM-facing glue layer that loads the trained model artifact, pulls recent DailyLog rows,
and persists the result — the same role ``train_forecast_model.py`` plays for training and
``recommendations/engine.py`` plays for the rule engine.
"""

import logging
import subprocess
import sys
from datetime import datetime
from decimal import Decimal

import joblib
import numpy as np
import pandas as pd
from django.conf import settings
from django.db import transaction

from farm.models import DailyLog
from recommendations.engine import generate_recommendations

from . import pipeline as ml
from .models import Forecast

logger = logging.getLogger(__name__)

MODEL_PATH = settings.BASE_DIR / "models" / "forecast_model.joblib"
RETRAIN_LOG_PATH = settings.BASE_DIR / "models" / "retrain.log"


class ModelNotTrainedError(Exception):
    """Raised when generate_forecast is called before train_forecast_model has ever run."""


def _load_artifact(model_path) -> dict:
    if not model_path.exists():
        raise ModelNotTrainedError(
            f"No trained model at {model_path}. Run `python manage.py train_forecast_model` first."
        )
    return joblib.load(model_path)


def _build_feature_row(daily_log: DailyLog):
    """Single-row MODEL_FEATURES-ordered input for .predict(), plus the prior logs used
    for lag1/roll3 (kept so the resulting Forecast can be traced back to its inputs).

    Priors are looked up the same way pipeline.add_lag_features builds them at training
    time: up to the 3 most recent DailyLogs in the same flock/caging_period, dated before
    this one. Fewer than 3 (e.g. the first log of a caging period) is a real, expected
    case — not an error — so missing lag1/roll3 become NaN, which the trained pipeline's
    SimpleImputer fills in.
    """
    priors = list(
        DailyLog.objects.filter(
            flock=daily_log.flock,
            caging_period=daily_log.caging_period,
            date__lt=daily_log.date,
        ).order_by("-date")[:3]
    )
    lag1 = float(priors[0].egg_count) if priors else np.nan
    roll3 = (sum(float(p.egg_count) for p in priors) / len(priors)) if priors else np.nan

    row = {
        "flock_size": float(daily_log.flock_size),
        "flock_age_weeks": float(daily_log.flock_age_weeks),
        "feed_intake_kg": float(daily_log.feed_intake_kg),
        "temperature_c": float(daily_log.temperature_c),
        "humidity_pct": float(daily_log.humidity_pct),
        "lag1": lag1,
        "roll3": roll3,
    }
    X = pd.DataFrame([row], columns=ml.MODEL_FEATURES).astype("float64")
    return X, priors


def generate_forecast(daily_log: DailyLog, model_path=None) -> Forecast:
    """Predict daily + tri-day yield for daily_log's own date and persist a Forecast.

    forecast_date is set to daily_log.date (not the day after) — the daily/tri-day models
    are trained to predict a day's own yield (and the following 3-day sum) from that day's
    own farm conditions plus recent laying history, so this is a same-day "nowcast" using
    the reading the farmer just entered. dashboard/views.py's trend chart already expects
    this: it shows "actual" (DailyLog.egg_count) and "predicted" (Forecast.predicted_daily_yield)
    side by side for the same date.

    update_or_create keeps this idempotent (re-generating for a date already forecast
    updates it in place rather than erroring against the flock+forecast_date uniqueness
    constraint). Regenerates the Forecast's Recommendations as the final step.

    Raises ModelNotTrainedError if no model artifact exists yet. Any other error (corrupt
    artifact, shape mismatch, etc.) propagates — callers that must not lose the DailyLog
    save (farm.views.log_daily_data) should catch broadly around this call.
    """
    artifact = _load_artifact(model_path or MODEL_PATH)
    X, priors = _build_feature_row(daily_log)

    daily_pred = max(float(artifact["daily_pipeline"].predict(X)[0]), 0.0)
    tri_pred = max(float(artifact["tri_day_pipeline"].predict(X)[0]), 0.0)

    with transaction.atomic():
        forecast, _ = Forecast.objects.update_or_create(
            flock=daily_log.flock,
            forecast_date=daily_log.date,
            defaults={
                "predicted_daily_yield": Decimal(str(round(daily_pred, 2))),
                "predicted_tri_day_yield": Decimal(str(round(tri_pred, 2))),
                "feature_importances": artifact["feature_importances"]["daily"],
                "model_version": artifact["model_version"],
            },
        )
        forecast.source_logs.set([daily_log, *priors])
        generate_recommendations(forecast)

    return forecast


def trigger_retrain(reason: str) -> None:
    """Fire-and-forget a background `train_forecast_model --tune --strict` run.

    Called right after a DailyLog write closes out a caging period or retires a flock —
    the two points where a genuinely new, complete segment of training data exists (see
    itikcare-spec.md section 5's "rolling retraining as new data comes in"). Calendar-based
    retraining doesn't fit this farm's data-generation rhythm: it logs roughly a row a day,
    so a fixed interval would often fire with nothing new to learn from.

    Runs as a detached subprocess rather than inline because a --tune search is too slow
    (single-digit-to-low-tens of seconds) to run synchronously inside a request/response
    cycle. --strict is always used so a bad or unlucky retrain can never overwrite the
    model currently serving real predictions — it only persists if every acceptance
    threshold still passes; the previous working artifact is left untouched otherwise.

    Never raises: launching the subprocess is best-effort, logged on failure, so a problem
    here can never break the view that triggered it.
    """
    try:
        RETRAIN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(RETRAIN_LOG_PATH, "a", encoding="utf-8") as log_fh:
            log_fh.write(f"\n=== retrain triggered: reason={reason} at {datetime.now().isoformat(timespec='seconds')} ===\n")
            log_fh.flush()
            subprocess.Popen(
                [sys.executable, str(settings.BASE_DIR / "manage.py"), "train_forecast_model", "--tune", "--strict"],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )
    except Exception:
        logger.exception("Failed to launch background retrain (reason=%s)", reason)
