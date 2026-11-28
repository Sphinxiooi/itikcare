"""Repeatable Random Forest training/retraining pipeline — one model per farmer.

Run as:
    python manage.py train_forecast_model --owner-id ID [--dry-run] [--strict]
                                          [--n-estimators N] [--test-fraction F]
                                          [--output-dir DIR]
                                          [--tune [--tune-iter N] [--cv-folds K]
                                                  [--fallback-untuned]]
                                          [--force] [--gap-fill | --no-gap-fill]

There is no single global model: every farmer (User) gets a separate artifact, and
``--owner-id`` says whose. The foundation farmer (accounts.User.is_foundation_farmer,
Mario in this deployment) trains on only their own historical DailyLogs, exactly as
before multi-tenancy. Every other farmer's model is bootstrapped from, and every retrain
refits fresh on, the foundation farmer's data concatenated with their own accumulated
DailyLogs (never sklearn warm_start — a brand-new .fit() each time on the full pooled
set) — see ``_load_records``.

``--tune`` replaces the fixed ``build_estimator(n_estimators)`` fit with a randomized
hyperparameter search (``pipeline.tune_estimator``) scored on an inner, segment-aware CV
carved out of the training partition only — the held-out 15% test set is never touched
during the search, only for the final metrics/threshold check. Omit ``--tune`` for the
original fast, fixed-hyperparameter path (unchanged default behaviour) — this is what
the signup flow uses for a new farmer's synchronous bootstrap train.

``--fallback-untuned`` (only meaningful with ``--tune``): a tuned search can overfit its
inner CV on a small dataset and land a model that misses an acceptance threshold the
plain fixed-hyperparameter fit clears. With this flag, a tuned run that misses any
threshold is discarded and the whole train/evaluate step is redone untuned; that model
is then persisted if it passes (or refused under ``--strict``, leaving the previous
artifact in place). A tuned model that passes everything is always preferred. This is
what the automatic retrain (forecasting/services.py::trigger_retrain) uses.

``--gap-fill`` (default: ``settings.FORECAST_GAP_FILL``, off) trains on the real logs plus
filled free-range gaps — see ``forecasting/gapfill.py``. The test rows and the threshold
means still come from real logs only. If either hybrid model misses any threshold, the whole
run is redone on real logs alone. The champion/challenger check below applies unchanged, so
switching an existing farmer over needs one ``--force`` run if the hybrid model passes but
does not beat the saved one.

Per CLAUDE.md, retraining must be a repeatable command (not a one-off notebook) so it
can run periodically as new DailyLog data comes in. This command is the orchestration
layer only — the modelling logic lives in ``forecasting/pipeline.py`` and is unit-tested
there. Stages:

1. Load this owner's DailyLogs (plus the foundation farmer's, unless the owner *is* the
   foundation farmer) via the ORM, ordered by owner then generation then date.
2. Segment on the composite segment_key (flock_id + caging_period — see pipeline.py's
   SEGMENT_COLUMN comment for why raw caging_period alone isn't safe once more than one
   flock/farm is pooled) and build the daily and tri-day datasets. The tri-day target is
   a 3-day forward sum that never spans a caging-period gap (itikcare-spec.md §10).
3. Chronological 85:15 split within each segment (adviser-approved 2026-09-06, was 80:20
   — see pipeline.chronological_split's docstring). With gap filling on, the training side
   is then rebuilt from real plus filled days; the test side is left as it is.
4. Fit a RandomForestRegressor for daily yield and a separate one for tri-day yield.
5. Evaluate both against the §5 thresholds (MAE ≤ 8%, RMSE ≤ 10%, MAPE ≤ 15%, R² ≥ 0.75),
   alongside a mean-predictor baseline.
6. Champion/challenger check: if this owner already has a saved model, score it on the
   *same* held-out test rows and only let the new model replace it if the new one beats
   it — a strictly lower MAE on both the daily and the tri-day test set. Otherwise the
   current model is kept untouched (and no records are locked). See
   ``_compare_with_current_model``. ``--force`` skips this check for a deliberate manual
   replacement; with no saved model yet (e.g. signup's bootstrap train) there is nothing
   to beat, so the new model is saved as long as the thresholds allow it.
7. Persist both pipelines + metadata + feature importances (joblib, atomically written)
   and a human-readable metrics report (JSON), so Forecast.model_version /
   feature_importances can be populated at prediction time and the prescriptive module
   can prioritise by importance.
"""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import joblib
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from farm.models import DailyLog
from forecasting import gapfill
from forecasting import pipeline as ml


class Command(BaseCommand):
    help = "Train or retrain the Random Forest egg yield forecasting model for one farmer."

    def add_arguments(self, parser):
        parser.add_argument(
            "--owner-id", type=int, required=True,
            help=(
                "User id to train/retrain a model for. There is no single global model "
                "any more — every farmer gets a separate artifact (see "
                "forecasting/services.py's model_path_for). Training data is that "
                "owner's own DailyLogs, plus the foundation farmer's (see "
                "accounts.User.is_foundation_farmer) if the owner isn't the foundation "
                "farmer themselves."
            ),
        )
        parser.add_argument(
            "--n-estimators", type=int, default=300,
            help="Number of trees in each Random Forest (default: 300).",
        )
        parser.add_argument(
            "--test-fraction", type=float, default=0.15,
            help="Fraction of each caging period held out as the chronological test set "
                 "(default: 0.15 — the adviser-approved 85:15 split, see "
                 "pipeline.chronological_split).",
        )
        parser.add_argument(
            "--output-dir", default=str(settings.MODEL_DIR),
            help="Directory the model artifact and metrics report are written to.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Train and report metrics, but write no files.",
        )
        parser.add_argument(
            "--strict", action="store_true",
            help="Refuse to persist the model if any acceptance threshold fails.",
        )
        parser.add_argument(
            "--tune", action="store_true",
            help=(
                "Replace the fixed-hyperparameter fit with a randomized search "
                "(see pipeline.tune_estimator), scored on an inner CV within the "
                "training partition only."
            ),
        )
        parser.add_argument(
            "--tune-iter", type=int, default=120,
            help=(
                "Number of candidate hyperparameter combinations to sample when --tune is "
                "set (default: 120 — the value validated to clear all four acceptance "
                "thresholds with a comfortable margin on this dataset; see pipeline.py's "
                "PARAM_DISTRIBUTIONS)."
            ),
        )
        parser.add_argument(
            "--cv-folds", type=int, default=4,
            help="Number of inner CV folds used to score candidates when --tune is set (default: 4).",
        )
        parser.add_argument(
            "--fallback-untuned", action="store_true",
            help=(
                "With --tune: if the tuned models miss any acceptance threshold, discard "
                "them and retrain with the fixed default hyperparameters instead, "
                "persisting that model if it passes. Ignored without --tune."
            ),
        )
        parser.add_argument(
            "--force", action="store_true",
            help=(
                "Skip the champion/challenger check and replace the owner's current model "
                "even if the new one doesn't beat it (the --strict threshold check still "
                "applies). For deliberate manual replacements only."
            ),
        )
        parser.add_argument(
            "--gap-fill", action=argparse.BooleanOptionalAction, default=None,
            help=(
                "Train on real logs plus filled free-range gaps (see forecasting/"
                "gapfill.py), falling back to real logs if any threshold is missed. "
                "Default: the FORECAST_GAP_FILL setting."
            ),
        )

    def handle(self, *args, **options):
        owner_id = options["owner_id"]
        try:
            owner = User.objects.get(pk=owner_id)
        except User.DoesNotExist:
            raise CommandError(f"No user with id={owner_id}.")

        records = self._load_records(owner)
        if len(records) < 10:
            raise CommandError(
                f"Not enough DailyLog rows to train for owner_id={owner_id} "
                f"({len(records)} found). Import/log data first."
            )

        df = ml.build_feature_frame(records)

        gap_fill = options["gap_fill"]
        if gap_fill is None:
            gap_fill = settings.FORECAST_GAP_FILL

        result = self._train_with_fallback(df, options, gap_fill)
        fell_back_from_hybrid = False
        if gap_fill and not result["all_pass"]:
            self.stdout.write(self.style.WARNING(
                "\nThe gap-filled models missed at least one threshold; discarding them and "
                "retraining on real logs only."
            ))
            result = self._train_with_fallback(df, options, gap_fill=False)
            fell_back_from_hybrid = True

        tuned = result["tuned"]
        daily_model, tri_model = result["daily_model"], result["tri_model"]
        daily_metrics, tri_metrics = result["daily_metrics"], result["tri_metrics"]
        daily_importances, tri_importances = result["daily_importances"], result["tri_importances"]
        all_pass = result["all_pass"]

        # --- Champion/challenger: does the new model beat the one currently in use? ---
        comparison = None
        if not options["force"]:
            comparison = self._compare_with_current_model(options["output_dir"], owner_id, result)

        # --- Persist -----------------------------------------------------------------
        model_version = f"rf-{owner_id}-{datetime.now():%Y%m%d-%H%M%S}"
        metrics_payload = {
            "model_version": model_version,
            "owner_id": owner_id,
            "trained_at": datetime.now().isoformat(timespec="seconds"),
            "n_samples_total": len(records),
            "features": ml.MODEL_FEATURES,
            "thresholds": ml.THRESHOLDS,
            "tuned": tuned,
            "fell_back_from_tuned": result["fell_back_from_tuned"],
            # "hybrid" = real logs plus filled gaps; "real" = real logs only.
            "data_method": result["data_method"],
            "gap_fill_requested": gap_fill,
            "fell_back_from_hybrid": fell_back_from_hybrid,
            "synthetic_days": result["synthetic_days"],
            "training_rows": result["training_rows"],
            "best_params": {"daily": result["daily_best_params"], "tri_day": result["tri_best_params"]},
            "daily": {"metrics": daily_metrics, "baseline": result["daily_baseline"],
                      "same_as_yesterday": result["daily_persistence"],
                      "feature_importances": daily_importances},
            "tri_day": {"metrics": tri_metrics, "baseline": result["tri_baseline"],
                        "same_as_yesterday": result["tri_persistence"],
                        "feature_importances": tri_importances},
            # Which model this one replaced and by how much it beat it (None when there
            # was no comparable model to beat, or --force was used).
            "replaced_model": comparison,
        }

        if options["dry_run"]:
            self.stdout.write(self.style.NOTICE("\n[DRY RUN] Nothing written."))
            return
        if options["strict"] and not all_pass:
            raise CommandError(
                "--strict is set and at least one threshold failed; refusing to persist the model."
            )
        if comparison is not None and not comparison["new_model_wins"]:
            # Not an error: keeping a better model in place is the intended outcome.
            # Records stay unlocked because no persisted model has learned from them yet.
            self.stdout.write(self.style.WARNING(
                f"\nThe new model did not beat the current model "
                f"({comparison['current_model_version']}); keeping the current model. "
                f"Nothing written."
            ))
            return

        self._persist(options["output_dir"], owner_id, model_version, metrics_payload,
                      daily_model, tri_model, daily_importances, tri_importances, len(records))
        self._lock_records(owner)

    def _train_with_fallback(self, df, options, gap_fill):
        """_train_all, redone untuned if --tune --fallback-untuned and a threshold fails."""
        tune = options["tune"]
        result = self._train_all(df, options, tune, gap_fill)

        fell_back_from_tuned = False
        if tune and not result["all_pass"] and options["fallback_untuned"]:
            self.stdout.write(self.style.WARNING(
                "\nThe tuned models missed at least one threshold; discarding them and "
                "retraining with the fixed default hyperparameters (--fallback-untuned)."
            ))
            result = self._train_all(df, options, False, gap_fill)
            tune = False
            fell_back_from_tuned = True

        return {**result, "tuned": tune, "fell_back_from_tuned": fell_back_from_tuned}

    def _train_all(self, df, options, tune, gap_fill):
        """Fit, evaluate and report both the daily and the tri-day model once.

        Pure orchestration of pipeline.py's and gapfill.py's functions, with no
        persistence side effects, so handle() can call it again for the fallbacks. The
        chronological split is made on real rows and is deterministic, so every attempt
        (tuned or not, gap-filled or not) is scored on exactly the same held-out rows.
        """
        feats = ml.MODEL_FEATURES
        synthetic_days = {"daily": 0, "tri_day": 0}
        if gap_fill:
            self.stdout.write(self.style.NOTICE("\nTraining data: real logs plus filled gaps."))

        # --- Daily model -------------------------------------------------------------
        daily_df = ml.add_lag_features(df)
        daily_train, daily_test = ml.chronological_split(daily_df, options["test_fraction"])
        daily_mean = float(daily_df[ml.DAILY_TARGET].mean())
        if gap_fill:
            daily_train, synthetic_days["daily"] = gapfill.hybrid_training_frame(
                df, daily_test, ml.DAILY_TARGET,
            )
        daily_model, daily_best_params = self._fit(daily_train, feats, ml.DAILY_TARGET, options, tune)
        daily_metrics = ml.evaluate(
            daily_test[ml.DAILY_TARGET],
            daily_model.predict(daily_test[feats]),
            daily_mean,
        )
        daily_baseline = ml.baseline_metrics(
            daily_train[feats], daily_train[ml.DAILY_TARGET],
            daily_test[feats], daily_test[ml.DAILY_TARGET], daily_mean,
        )
        daily_importances = ml.feature_importances(daily_model)

        # --- Tri-day model -----------------------------------------------------------
        # Tri-day target first, then the same within-segment lag features on top.
        tri_df = ml.add_lag_features(ml.add_tri_day_target(df))
        tri_train, tri_test = ml.chronological_split(tri_df, options["test_fraction"])
        tri_mean = float(tri_df[ml.TRI_DAY_TARGET].mean())
        if gap_fill:
            tri_train, synthetic_days["tri_day"] = gapfill.hybrid_training_frame(
                df, tri_test, ml.TRI_DAY_TARGET,
            )
        tri_model, tri_best_params = self._fit(tri_train, feats, ml.TRI_DAY_TARGET, options, tune)
        tri_metrics = ml.evaluate(
            tri_test[ml.TRI_DAY_TARGET],
            tri_model.predict(tri_test[feats]),
            tri_mean,
        )
        tri_baseline = ml.baseline_metrics(
            tri_train[feats], tri_train[ml.TRI_DAY_TARGET],
            tri_test[feats], tri_test[ml.TRI_DAY_TARGET], tri_mean,
        )
        tri_importances = ml.feature_importances(tri_model)

        # "Same as yesterday" (3 x yesterday for tri-day), reported next to the model so
        # its accuracy is always read against this simple rule.
        daily_persistence = ml.evaluate(daily_test[ml.DAILY_TARGET], daily_test["lag1"], daily_mean)
        tri_persistence = ml.evaluate(tri_test[ml.TRI_DAY_TARGET], 3 * tri_test["lag1"], tri_mean)

        # --- Report ------------------------------------------------------------------
        self._report("DAILY yield", len(daily_train), daily_metrics, daily_baseline,
                     daily_persistence, daily_importances, daily_best_params)
        self._report("TRI-DAY yield", len(tri_train), tri_metrics, tri_baseline,
                     tri_persistence, tri_importances, tri_best_params)
        if gap_fill:
            self.stdout.write(
                f"\nFilled (synthetic) days used for training: daily {synthetic_days['daily']}, "
                f"tri-day {synthetic_days['tri_day']}."
            )

        all_pass = all(daily_metrics["passes"].values()) and all(tri_metrics["passes"].values())
        if all_pass:
            self.stdout.write(self.style.SUCCESS("\nAll acceptance thresholds met for both models."))
        else:
            self.stdout.write(self.style.WARNING("\nSome acceptance thresholds were NOT met (see above)."))

        return {
            "all_pass": all_pass,
            "data_method": "hybrid" if gap_fill else "real",
            "synthetic_days": synthetic_days,
            "training_rows": {"daily": len(daily_train), "tri_day": len(tri_train)},
            "daily_persistence": daily_persistence, "tri_persistence": tri_persistence,
            "daily_model": daily_model, "daily_metrics": daily_metrics,
            "daily_baseline": daily_baseline, "daily_importances": daily_importances,
            "daily_best_params": daily_best_params,
            "tri_model": tri_model, "tri_metrics": tri_metrics,
            "tri_baseline": tri_baseline, "tri_importances": tri_importances,
            "tri_best_params": tri_best_params,
            # Kept so _compare_with_current_model can score the current saved model on
            # exactly the same held-out rows the new model was scored on.
            "daily_test": daily_test, "daily_mean": daily_mean,
            "tri_test": tri_test, "tri_mean": tri_mean,
        }

    def _compare_with_current_model(self, output_dir, owner_id, result):
        """Score the owner's currently saved model on the new model's held-out test rows.

        Returns None when there is nothing comparable to beat (no saved model yet, a file
        that can't be loaded, or a model built on a different feature list) — the new
        model may then replace it. Otherwise returns a dict with both models' MAE and
        `new_model_wins`, which is True only if the new model has a strictly lower MAE
        on BOTH the daily and the tri-day test set. A tie keeps the current model.

        Why this is a fair comparison: the split is chronological within each caging
        period and segments only grow at the end, so today's test rows (the last 15% of
        each segment) come after the rows the current model was trained on — it hasn't
        seen them either. If anything overlapped, it would favour the current model,
        i.e. make it harder for the new one to win, never easier.

        MAE is the deciding metric because it reads directly as "eggs per day off" and
        is the same error the §5 MAE threshold is built on.
        """
        model_path = Path(output_dir) / f"forecast_model_{owner_id}.joblib"
        if not model_path.exists():
            return None

        try:
            current = joblib.load(model_path)
            current_features = list(current["feature_names"])
            current_daily, current_tri = current["daily_pipeline"], current["tri_day_pipeline"]
        except Exception as exc:  # A broken artifact can't serve forecasts anyway.
            self.stdout.write(self.style.WARNING(
                f"\nCurrent model at {model_path} could not be loaded ({exc!r}); "
                f"it will be replaced if the new model is saved."
            ))
            return None

        if current_features != list(ml.MODEL_FEATURES):
            self.stdout.write(self.style.WARNING(
                "\nCurrent model was trained on a different feature list, so it can't be "
                "scored on the same test rows; it will be replaced if the new model is saved."
            ))
            return None

        feats = ml.MODEL_FEATURES
        daily_test, tri_test = result["daily_test"], result["tri_test"]
        current_daily_mae = ml.evaluate(
            daily_test[ml.DAILY_TARGET], current_daily.predict(daily_test[feats]), result["daily_mean"],
        )["mae"]
        current_tri_mae = ml.evaluate(
            tri_test[ml.TRI_DAY_TARGET], current_tri.predict(tri_test[feats]), result["tri_mean"],
        )["mae"]
        new_daily_mae = result["daily_metrics"]["mae"]
        new_tri_mae = result["tri_metrics"]["mae"]

        daily_wins = new_daily_mae < current_daily_mae
        tri_wins = new_tri_mae < current_tri_mae
        current_version = current.get("model_version", "unknown")

        verdict = lambda won: self.style.SUCCESS("NEW WINS") if won else self.style.ERROR("CURRENT KEPT")
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== New model vs current model ({current_version}) — MAE on the same test rows ==="
        ))
        self.stdout.write(
            f"  DAILY    current {current_daily_mae:7.2f}   new {new_daily_mae:7.2f}   [{verdict(daily_wins)}]"
        )
        self.stdout.write(
            f"  TRI-DAY  current {current_tri_mae:7.2f}   new {new_tri_mae:7.2f}   [{verdict(tri_wins)}]"
        )

        return {
            "current_model_version": current_version,
            "daily": {"current_mae": current_daily_mae, "new_mae": new_daily_mae},
            "tri_day": {"current_mae": current_tri_mae, "new_mae": new_tri_mae},
            "new_model_wins": daily_wins and tri_wins,
        }

    def _load_records(self, owner):
        """Pull this owner's training DailyLogs into plain dicts (ORM stays out of pipeline.py).

        The foundation farmer (accounts.User.is_foundation_farmer) is training on only
        their own historical data — unchanged behaviour from the single-tenant system.
        Every other farmer's model is bootstrapped from + retrained on the foundation
        farmer's data concatenated with their own, fresh each time (never warm_start),
        per the multi-tenant plan's model-strategy decision.
        """
        owner_ids = {owner.id}
        if not owner.is_foundation_farmer:
            owner_ids.add(User.get_foundation_farmer().id)

        rows = (
            DailyLog.objects
            .filter(flock__owner_id__in=owner_ids)
            .order_by("flock__owner_id", "flock__generation_number", "date")
            .values(
                "date", "flock_id", ml.CAGING_PERIOD_COLUMN, *ml.FEATURES, ml.DAILY_TARGET,
                gapfill.OWNER_COLUMN,  # gap filling fits its weather imputer per farm
            )
        )
        return list(rows)

    def _lock_records(self, owner):
        """Lock every DailyLog currently on file for `owner`'s own flocks, now that a
        model has been successfully persisted having read all of them in this run —
        every retrain is a full refit over the owner's whole DailyLog history (see
        this module's docstring), so a persisted model has already learned from
        whatever these rows currently hold.

        Scoped to owner.id alone, never the pooled owner_ids set `_load_records` reads
        from — otherwise a downstream farmer's retrain (which also reads the
        foundation farmer's rows for bootstrapping) would incorrectly lock the
        foundation farmer's own DailyLogs too.
        """
        DailyLog.objects.filter(flock__owner_id=owner.id).update(is_locked=True)

    def _fit(self, train_df, feats, target, options, tune):
        """Fit one model, either with fixed hyperparameters or --tune's randomized search.

        `tune` is passed explicitly (not read from options) because --fallback-untuned
        fits the same data a second time with tuning off. Returns (fitted_pipeline,
        best_params) where best_params is None on the untuned path (nothing to report).
        """
        if not tune:
            model = ml.build_estimator(options["n_estimators"])
            model.fit(train_df[feats], train_df[target])
            return model, None

        model, best_params = ml.tune_estimator(
            train_df, feats, target,
            n_splits=options["cv_folds"], n_iter=options["tune_iter"],
        )
        return model, best_params

    def _report(self, title, n_train, metrics, baseline, persistence, importances,
                best_params=None):
        thr = ml.THRESHOLDS
        ok = lambda passed: self.style.SUCCESS("PASS") if passed else self.style.ERROR("FAIL")
        p = metrics["passes"]
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== {title} ==="))
        self.stdout.write(
            f"  train / test rows: {n_train} / {metrics['n']}   "
            f"target mean: {metrics['target_mean']:.1f}"
        )
        self.stdout.write(
            f"  MAE   {metrics['mae']:7.2f}  ({metrics['mae_pct'] * 100:5.2f}% of mean, "
            f"threshold <= {thr['mae_pct'] * 100:.0f}%)   [{ok(p['mae'])}]"
        )
        self.stdout.write(
            f"  RMSE  {metrics['rmse']:7.2f}  ({metrics['rmse_pct'] * 100:5.2f}% of mean, "
            f"threshold <= {thr['rmse_pct'] * 100:.0f}%)   [{ok(p['rmse'])}]"
        )
        self.stdout.write(
            f"  MAPE  {metrics['mape'] * 100:6.2f}%  (threshold <= {thr['mape'] * 100:.0f}%)"
            f"           [{ok(p['mape'])}]"
        )
        self.stdout.write(
            f"  R2    {metrics['r2']:7.3f}  (threshold >= {thr['r2']})"
            f"              [{ok(p['r2'])}]"
        )
        self.stdout.write(
            f"  baseline (mean predictor): MAE {baseline['mae']:.2f}, R2 {baseline['r2']:.3f}"
        )
        self.stdout.write(
            f"  baseline (same as yesterday): MAE {persistence['mae']:.2f}, "
            f"R2 {persistence['r2']:.3f}"
        )
        self.stdout.write("  feature importances:")
        for feature, importance in importances.items():
            self.stdout.write(f"    {feature:18s} {importance:.3f}")
        if best_params is not None:
            self.stdout.write("  best hyperparameters (--tune):")
            for name, value in best_params.items():
                self.stdout.write(f"    {name:22s} {value}")

    def _persist(self, output_dir, owner_id, model_version, metrics_payload,
                 daily_model, tri_model, daily_importances, tri_importances, n_samples):
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        artifact = {
            "model_version": model_version,
            "owner_id": owner_id,
            "trained_at": metrics_payload["trained_at"],
            "feature_names": ml.MODEL_FEATURES,
            "n_samples": n_samples,  # real DailyLogs; filled days are never counted
            "data_method": metrics_payload["data_method"],
            "daily_pipeline": daily_model,
            "tri_day_pipeline": tri_model,
            "feature_importances": {"daily": daily_importances, "tri_day": tri_importances},
            "metrics": {"daily": metrics_payload["daily"]["metrics"],
                        "tri_day": metrics_payload["tri_day"]["metrics"]},
        }
        model_path = out / f"forecast_model_{owner_id}.joblib"
        metrics_path = out / f"forecast_metrics_{owner_id}.json"

        # Multi-tenancy means several owners' retrains can now be triggered close
        # together (each farm has its own caging_period_closed/flock_retired events).
        # Write to a temp file in the same directory and atomically replace, so a
        # concurrent generate_forecast() read can only ever see a complete old or
        # complete new artifact for this owner, never a half-written one.
        tmp_model_path = model_path.with_suffix(model_path.suffix + ".tmp")
        tmp_metrics_path = metrics_path.with_suffix(metrics_path.suffix + ".tmp")
        joblib.dump(artifact, tmp_model_path)
        tmp_metrics_path.write_text(json.dumps(metrics_payload, indent=2), encoding="utf-8")
        os.replace(tmp_model_path, model_path)
        os.replace(tmp_metrics_path, metrics_path)

        self.stdout.write(self.style.SUCCESS(
            f"\nSaved model {model_version} -> {model_path}\nSaved metrics -> {metrics_path}"
        ))
