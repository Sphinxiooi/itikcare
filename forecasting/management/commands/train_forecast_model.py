"""Repeatable Random Forest training/retraining pipeline.

Run as:
    python manage.py train_forecast_model [--dry-run] [--strict]
                                          [--n-estimators N] [--test-fraction F]
                                          [--output-dir DIR]
                                          [--tune [--tune-iter N] [--cv-folds K]]

``--tune`` replaces the fixed ``build_estimator(n_estimators)`` fit with a randomized
hyperparameter search (``pipeline.tune_estimator``) scored on an inner, segment-aware CV
carved out of the training partition only — the held-out 20% test set is never touched
during the search, only for the final metrics/threshold check. Omit ``--tune`` for the
original fast, fixed-hyperparameter path (unchanged default behaviour).

Per CLAUDE.md, retraining must be a repeatable command (not a one-off notebook) so it
can run periodically as new DailyLog data comes in. This command is the orchestration
layer only — the modelling logic lives in ``forecasting/pipeline.py`` and is unit-tested
there. Stages:

1. Load every DailyLog via the ORM (the rolling-retraining source: it retrains on
   whatever is in the database now), ordered by generation then date.
2. Segment on caging_period and build the daily and tri-day datasets. The tri-day target
   is a 3-day forward sum that never spans a caging-period gap (itikcare-spec.md §10).
3. Chronological 80:20 split within each caging period.
4. Fit a RandomForestRegressor for daily yield and a separate one for tri-day yield.
5. Evaluate both against the §5 thresholds (MAE ≤ 8%, RMSE ≤ 10%, MAPE ≤ 15%, R² ≥ 0.75),
   alongside a mean-predictor baseline.
6. Persist both pipelines + metadata + feature importances (joblib) and a human-readable
   metrics report (JSON), so Forecast.model_version / feature_importances can be
   populated at prediction time and the prescriptive module can prioritise by importance.
"""

import json
from datetime import datetime

import joblib
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from farm.models import DailyLog
from forecasting import pipeline as ml


class Command(BaseCommand):
    help = "Train or retrain the Random Forest egg yield forecasting model."

    def add_arguments(self, parser):
        parser.add_argument(
            "--n-estimators", type=int, default=300,
            help="Number of trees in each Random Forest (default: 300).",
        )
        parser.add_argument(
            "--test-fraction", type=float, default=0.2,
            help="Fraction of each caging period held out as the chronological test set (default: 0.2).",
        )
        parser.add_argument(
            "--output-dir", default=str(settings.BASE_DIR / "models"),
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

    def handle(self, *args, **options):
        records = self._load_records()
        if len(records) < 10:
            raise CommandError(
                f"Not enough DailyLog rows to train ({len(records)} found). Import data first."
            )

        df = ml.build_feature_frame(records)
        feats = ml.MODEL_FEATURES

        # --- Daily model -------------------------------------------------------------
        daily_df = ml.add_lag_features(df)
        daily_train, daily_test = ml.chronological_split(daily_df, options["test_fraction"])
        daily_mean = float(daily_df[ml.DAILY_TARGET].mean())
        daily_model, daily_best_params = self._fit(daily_train, feats, ml.DAILY_TARGET, options)
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
        tri_model, tri_best_params = self._fit(tri_train, feats, ml.TRI_DAY_TARGET, options)
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

        # --- Report ------------------------------------------------------------------
        self._report("DAILY yield", len(daily_train), daily_metrics, daily_baseline, daily_importances, daily_best_params)
        self._report("TRI-DAY yield", len(tri_train), tri_metrics, tri_baseline, tri_importances, tri_best_params)

        all_pass = all(daily_metrics["passes"].values()) and all(tri_metrics["passes"].values())
        if all_pass:
            self.stdout.write(self.style.SUCCESS("\nAll acceptance thresholds met for both models."))
        else:
            self.stdout.write(self.style.WARNING("\nSome acceptance thresholds were NOT met (see above)."))

        # --- Persist -----------------------------------------------------------------
        model_version = f"rf-{datetime.now():%Y%m%d-%H%M%S}"
        metrics_payload = {
            "model_version": model_version,
            "trained_at": datetime.now().isoformat(timespec="seconds"),
            "n_samples_total": len(records),
            "features": ml.MODEL_FEATURES,
            "thresholds": ml.THRESHOLDS,
            "tuned": options["tune"],
            "best_params": {"daily": daily_best_params, "tri_day": tri_best_params},
            "daily": {"metrics": daily_metrics, "baseline": daily_baseline,
                      "feature_importances": daily_importances},
            "tri_day": {"metrics": tri_metrics, "baseline": tri_baseline,
                        "feature_importances": tri_importances},
        }

        if options["dry_run"]:
            self.stdout.write(self.style.NOTICE("\n[DRY RUN] Nothing written."))
            return
        if options["strict"] and not all_pass:
            raise CommandError(
                "--strict is set and at least one threshold failed; refusing to persist the model."
            )

        self._persist(options["output_dir"], model_version, metrics_payload,
                      daily_model, tri_model, daily_importances, tri_importances, len(records))

    def _load_records(self):
        """Pull DailyLogs into plain dicts for the pipeline (ORM stays out of pipeline.py)."""
        rows = (
            DailyLog.objects
            .order_by("flock__generation_number", "date")
            .values("date", ml.SEGMENT_COLUMN, *ml.FEATURES, ml.DAILY_TARGET)
        )
        return list(rows)

    def _fit(self, train_df, feats, target, options):
        """Fit one model, either with fixed hyperparameters or --tune's randomized search.

        Returns (fitted_pipeline, best_params) where best_params is None on the
        untuned path (there's nothing to report).
        """
        if not options["tune"]:
            model = ml.build_estimator(options["n_estimators"])
            model.fit(train_df[feats], train_df[target])
            return model, None

        model, best_params = ml.tune_estimator(
            train_df, feats, target,
            n_splits=options["cv_folds"], n_iter=options["tune_iter"],
        )
        return model, best_params

    def _report(self, title, n_train, metrics, baseline, importances, best_params=None):
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
        self.stdout.write("  feature importances:")
        for feature, importance in importances.items():
            self.stdout.write(f"    {feature:18s} {importance:.3f}")
        if best_params is not None:
            self.stdout.write("  best hyperparameters (--tune):")
            for name, value in best_params.items():
                self.stdout.write(f"    {name:22s} {value}")

    def _persist(self, output_dir, model_version, metrics_payload,
                 daily_model, tri_model, daily_importances, tri_importances, n_samples):
        from pathlib import Path

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        artifact = {
            "model_version": model_version,
            "trained_at": metrics_payload["trained_at"],
            "feature_names": ml.MODEL_FEATURES,
            "n_samples": n_samples,
            "daily_pipeline": daily_model,
            "tri_day_pipeline": tri_model,
            "feature_importances": {"daily": daily_importances, "tri_day": tri_importances},
            "metrics": {"daily": metrics_payload["daily"]["metrics"],
                        "tri_day": metrics_payload["tri_day"]["metrics"]},
        }
        model_path = out / "forecast_model.joblib"
        metrics_path = out / "forecast_metrics.json"
        joblib.dump(artifact, model_path)
        metrics_path.write_text(json.dumps(metrics_payload, indent=2), encoding="utf-8")

        self.stdout.write(self.style.SUCCESS(
            f"\nSaved model {model_version} -> {model_path}\nSaved metrics -> {metrics_path}"
        ))
