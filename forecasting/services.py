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
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import joblib
import numpy as np
import pandas as pd
from django.conf import settings
from django.db import transaction

from farm.models import DailyLog
from farm.services import get_effective_coordinates, operational_today
from farm.weather import fetch_forecast_weather
from recommendations.engine import generate_recommendations

from . import pipeline as ml
from .models import Forecast

logger = logging.getLogger(__name__)

MODEL_DIR = settings.MODEL_DIR
RETRAIN_LOG_PATH = MODEL_DIR / "retrain.log"


class ModelNotTrainedError(Exception):
    """Raised when generate_forecast is called before train_forecast_model has ever run."""


# --- Forecast warm-up ---------------------------------------------------------------
# The yield prediction is withheld (recommendations still run) until there's enough of
# the farmer's *own* data behind it:
#
# * NEW_FARM_MIN_LOGS -- a brand-new farmer's model is bootstrapped from the foundation
#   farm's history only (accounts/views.py::_bootstrap_train). A Random Forest can't
#   predict outside the range of yields it was trained on, so a much smaller (or larger)
#   flock gets a number anchored to the foundation farm's scale -- e.g. 154 eggs for a
#   50-duck flock. One week of logs is required before any prediction is shown.
# * RECAGE_WARMUP_LOGS -- the first logs of a new caging period (back from free-range, or
#   a new flock generation) have fewer than 3 prior days in the segment, so lag1/roll3
#   would be partly imputed (itikcare-spec.md section 10). Predictions resume on the log
#   *after* these, which is the first one with a full 3-day history behind it.
NEW_FARM_MIN_LOGS = 7
RECAGE_WARMUP_LOGS = 3


@dataclass(frozen=True)
class ForecastReadiness:
    """Whether a prediction may be shown yet, and if not, how far along the warm-up is.

    reason is "new_farm" or "recaged" while warming up, "" once ready. logs_so_far /
    logs_needed drive the farmer-facing "n/7 days logged" notice.
    """

    is_ready: bool
    reason: str = ""
    logs_so_far: int = 0
    logs_needed: int = 0


def forecast_readiness(owner, flock=None, caging_period=None, as_of=None) -> ForecastReadiness:
    """Apply the warm-up rule (see NEW_FARM_MIN_LOGS / RECAGE_WARMUP_LOGS above).

    Counts logs, not calendar days: a backfilled past day is real history that feeds
    lag1/roll3 just like one logged on time. as_of limits the count to logs dated on or
    before that day (so re-checking an older log gives the same answer it got then).
    The new-farm rule is checked first; the recage rule needs flock + caging_period.
    """
    owner_logs = DailyLog.objects.filter(flock__owner=owner)
    if as_of is not None:
        owner_logs = owner_logs.filter(date__lte=as_of)

    total = owner_logs.count()
    if total < NEW_FARM_MIN_LOGS:
        return ForecastReadiness(False, "new_farm", total, NEW_FARM_MIN_LOGS)

    if flock is not None and caging_period is not None:
        in_period = owner_logs.filter(flock=flock, caging_period=caging_period).count()
        if in_period <= RECAGE_WARMUP_LOGS:
            return ForecastReadiness(False, "recaged", in_period, RECAGE_WARMUP_LOGS + 1)

    return ForecastReadiness(True)


def readiness_for_log(daily_log: DailyLog) -> ForecastReadiness:
    """forecast_readiness as of one specific DailyLog."""
    return forecast_readiness(
        daily_log.flock.owner, daily_log.flock, daily_log.caging_period, as_of=daily_log.date
    )


def model_path_for(owner_id: int):
    """Per-owner model artifact path — there is no single global model any more."""
    return MODEL_DIR / f"forecast_model_{owner_id}.joblib"


# The most recently loaded model artifact in this worker process, reused while its file on
# disk is unchanged. Reading one back from disk takes ~0.1-0.2s on every daily-log save
# otherwise. Only one is kept, so memory stays bounded to one model per gunicorn worker
# (this is a single-farm app -- in practice every save uses the same farm's model).
_artifact_cache = {"key": None, "artifact": None}


def _load_artifact(model_path) -> dict:
    """Load the model artifact at model_path, reusing this process's cached copy when the
    file hasn't changed since it was loaded.

    "Unchanged" means same path, modification time, size and inode. Retraining
    (train_forecast_model.py) writes a new file and os.replace()s it into place, which
    changes all of those, so the very next forecast after a retrain loads the new model.
    Loading never changes what the model predicts -- the cached copy is the same object
    joblib.load would return, and prediction doesn't modify it. Always reads from disk
    when settings.PERFORMANCE_CACHES_ENABLED is off (the test runner).
    """
    if not model_path.exists():
        raise ModelNotTrainedError(
            f"No trained model at {model_path}. Run "
            f"`python manage.py train_forecast_model --owner-id <id>` first."
        )
    if not settings.PERFORMANCE_CACHES_ENABLED:
        return joblib.load(model_path)

    stat = model_path.stat()
    key = (str(model_path), stat.st_mtime_ns, stat.st_size, stat.st_ino)
    if _artifact_cache["key"] != key:
        artifact = joblib.load(model_path)
        _artifact_cache["artifact"] = artifact
        _artifact_cache["key"] = key
    return _artifact_cache["artifact"]


def current_forecast_warmup(active_flock):
    """The warm-up state to show on the dashboard / Forecast page / notification bell,
    or None when there's nothing to show (no flock, free-range, or already ready).

    Judged against the flock's most recent log (all logs, no as_of cut-off). A farmer
    with no logs yet at all still gets the new-farm "0/7" notice straight away.
    """
    if active_flock is None or not active_flock.is_caged:
        return None
    latest_log = DailyLog.objects.filter(flock=active_flock).order_by("-date").first()
    if latest_log is None:
        readiness = forecast_readiness(active_flock.owner)
    else:
        readiness = forecast_readiness(active_flock.owner, active_flock, latest_log.caging_period)
    return None if readiness.is_ready else readiness


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


def _predict_next_days(daily_pipeline, daily_log: DailyLog, priors, weather_by_date: dict) -> tuple[float, float, float]:
    """Recursively forecast forecast_date + 1/+2/+3 days by re-applying daily_pipeline
    three times, feeding each step's own prediction back in as the next step's lag1/roll3
    history feature -- standard iterative multi-step forecasting. Not a separately trained
    model: reuses the same daily_pipeline used for predicted_daily_yield.

    flock_size/flock_age_weeks/feed_intake_kg are carried forward unchanged from
    daily_log's own values for all 3 future days, since the farmer hasn't logged them yet.
    temperature_c/humidity_pct come from weather_by_date (keyed by real calendar date --
    see farm.weather.fetch_forecast_weather) when available, else are likewise carried
    forward from daily_log.

    From day+2 onward, roll3 is a mean of *predicted* (not actual) prior days, so
    prediction error compounds across the 3 steps -- day+3 is the least certain of the
    three. This is expected and worth being explicit about (thesis defense): these are
    best-effort projections, not equally-certain restatements of the same-day nowcast.

    history holds up to the 3 most recent known-or-predicted egg counts, most-recent
    first: starts as [today's actual, yesterday's actual, day-before's actual] (fewer if
    priors is short, e.g. a fresh caging period), then each new prediction is prepended
    before the next step, same "up to 3, not exactly 3" convention as add_lag_features.
    """
    history = [float(daily_log.egg_count)] + [float(p.egg_count) for p in priors[:2]]
    predictions = []
    for n in range(1, 4):
        target_date = daily_log.date + timedelta(days=n)
        weather = weather_by_date.get(target_date, {})
        temperature_c = weather.get("temperature_c", float(daily_log.temperature_c))
        humidity_pct = weather.get("humidity_pct", float(daily_log.humidity_pct))
        row = {
            "flock_size": float(daily_log.flock_size),
            "flock_age_weeks": float(daily_log.flock_age_weeks),
            "feed_intake_kg": float(daily_log.feed_intake_kg),
            "temperature_c": temperature_c,
            "humidity_pct": humidity_pct,
            "lag1": history[0],
            "roll3": sum(history[:3]) / len(history[:3]),
        }
        X = pd.DataFrame([row], columns=ml.MODEL_FEATURES).astype("float64")
        pred = max(float(daily_pipeline.predict(X)[0]), 0.0)
        predictions.append(pred)
        history.insert(0, pred)
    return tuple(predictions)


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

    Also persists a recursive best-effort day+1/day+2/day+3 breakdown (see
    _predict_next_days) for the dashboard's "Next 3-Day Forecast" panel — genuinely
    distinct per-day numbers, unlike predicted_tri_day_yield's 3-day sum above. Weather
    for those future days is only fetched when daily_log.date is today — operationally,
    not just by the calendar (see farm.services.operational_today: this farm's logging
    day rolls over at 6am, not midnight) — since Open-Meteo's forecast is anchored to
    real "now" and can't meaningfully inform a backdated log's future days; otherwise
    the recursion falls back to daily_log's own carried-forward temperature_c/humidity_pct.

    During a warm-up period (see forecast_readiness) every predicted_* field is left null
    and the model is never asked to predict -- only recommendations are generated.
    """
    artifact = _load_artifact(model_path or model_path_for(daily_log.flock.owner_id))
    X, priors = _build_feature_row(daily_log)

    if readiness_for_log(daily_log).is_ready:
        daily_pred = max(float(artifact["daily_pipeline"].predict(X)[0]), 0.0)
        tri_pred = max(float(artifact["tri_day_pipeline"].predict(X)[0]), 0.0)

        if daily_log.date == operational_today():
            lat, lon = get_effective_coordinates(daily_log.flock.owner)
            weather_by_date = fetch_forecast_weather(lat, lon)
        else:
            weather_by_date = {}
        next_days = _predict_next_days(artifact["daily_pipeline"], daily_log, priors, weather_by_date)
        predictions = {
            "predicted_daily_yield": daily_pred,
            "predicted_tri_day_yield": tri_pred,
            "predicted_next_day1_yield": next_days[0],
            "predicted_next_day2_yield": next_days[1],
            "predicted_next_day3_yield": next_days[2],
        }
        predictions = {field: Decimal(str(round(value, 2))) for field, value in predictions.items()}
    else:
        # Warm-up: no prediction at all (see forecast_readiness). The Forecast row is still
        # written so recommendations -- which only read the logged inputs and the model's
        # feature importances, never the predicted values -- keep working.
        predictions = dict.fromkeys(
            [
                "predicted_daily_yield",
                "predicted_tri_day_yield",
                "predicted_next_day1_yield",
                "predicted_next_day2_yield",
                "predicted_next_day3_yield",
            ]
        )

    with transaction.atomic():
        forecast, _ = Forecast.objects.update_or_create(
            flock=daily_log.flock,
            forecast_date=daily_log.date,
            defaults={
                **predictions,
                # A copy, so nothing done to the saved Forecast's dict can ever reach
                # the cached artifact (see _load_artifact).
                "feature_importances": dict(artifact["feature_importances"]["daily"]),
                "model_version": artifact["model_version"],
            },
        )
        forecast.source_logs.set([daily_log, *priors])
        generate_recommendations(forecast)

    return forecast


def trigger_retrain(reason: str, owner_id: int) -> None:
    """Fire-and-forget a background `train_forecast_model --owner-id ID --tune --fallback-untuned --strict` run.

    Called right after a DailyLog write closes out a caging period or retires a flock —
    the two points where a genuinely new, complete segment of training data exists (see
    itikcare-spec.md section 5's "rolling retraining as new data comes in"). Calendar-based
    retraining doesn't fit this farm's data-generation rhythm: it logs roughly a row a day,
    so a fixed interval would often fire with nothing new to learn from.

    ``owner_id`` scopes the retrain to one farmer's model only — each farm's own
    caging_period_closed/flock_retired events fire independently now, so this must never
    retrain (or overwrite the artifact of) every farm at once.

    Runs as a detached subprocess rather than inline because a --tune search is too slow
    (single-digit-to-low-tens of seconds) to run synchronously inside a request/response
    cycle. --strict is always used so a bad or unlucky retrain can never overwrite the
    model currently serving real predictions — it only persists if every acceptance
    threshold still passes; the previous working artifact is left untouched otherwise.
    (Contrast with the signup flow's bootstrap train, which runs synchronously and
    untuned — see accounts/views.py::signup — because that one must finish before the
    farmer's very first daily log can be forecast.)

    Never raises: launching the subprocess is best-effort, logged on failure, so a problem
    here can never break the view that triggered it.
    """
    try:
        RETRAIN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(RETRAIN_LOG_PATH, "a", encoding="utf-8") as log_fh:
            log_fh.write(
                f"\n=== retrain triggered: reason={reason} owner_id={owner_id} "
                f"at {datetime.now().isoformat(timespec='seconds')} ===\n"
            )
            log_fh.flush()
            subprocess.Popen(
                [
                    sys.executable, str(settings.BASE_DIR / "manage.py"), "train_forecast_model",
                    "--owner-id", str(owner_id), "--tune", "--fallback-untuned", "--strict",
                ],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )
    except Exception:
        logger.exception("Failed to launch background retrain (reason=%s, owner_id=%s)", reason, owner_id)
