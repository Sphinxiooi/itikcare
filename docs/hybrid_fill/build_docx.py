"""Build the Word version of the hybrid gap-filling documentation.

Run after hybrid_fill_experiment.py, check_triday_leak.py and build_report.py:
    venv/Scripts/python docs/hybrid_fill/build_docx.py
"""
import json
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

HERE = Path(__file__).resolve().parent
R = json.loads((HERE / "results.json").read_text(encoding="utf-8"))
LEAK = json.loads((HERE / "triday_leak_check.json").read_text(encoding="utf-8"))
T = R["targets"]
HORIZONS = {"daily": "Daily", "triday": "Tri-day"}

doc = Document()
page = doc.sections[0]
page.page_width, page.page_height = Inches(8.5), Inches(11)
page.left_margin = page.right_margin = page.top_margin = page.bottom_margin = Inches(1)
for name, size in (("Normal", 11), ("Title", 22), ("Heading 1", 15), ("Heading 2", 12.5)):
    style = doc.styles[name]
    style.font.name = "Calibri"
    style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "Calibri")
    style.font.size = Pt(size)
    if name != "Normal":
        style.font.color.rgb = RGBColor(0x14, 0x4F, 0x44)

H1, P = lambda t: doc.add_heading(t, 1), doc.add_paragraph
H2 = lambda t: doc.add_heading(t, 2)


def bullets(items, style="List Bullet"):
    for item in items:
        doc.add_paragraph(item, style=style)


def shade(cell, colour):
    fill = OxmlElement("w:shd")
    fill.set(qn("w:val"), "clear")
    fill.set(qn("w:fill"), colour)
    cell._tc.get_or_add_tcPr().append(fill)


def table(head, rows, widths):
    t = doc.add_table(rows=1, cols=len(head), style="Table Grid")
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    for i, values in enumerate([head] + rows):
        row = t.rows[0] if i == 0 else t.add_row()
        no_split = OxmlElement("w:cantSplit")
        row._tr.get_or_add_trPr().append(no_split)
        if i == 0:  # repeat the header if the table crosses a page
            row._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
        for cell, value, width in zip(row.cells, values, widths):
            cell.width = Inches(width)
            cell.text = str(value)
            for run in cell.paragraphs[0].runs:
                run.font.size = Pt(9.5)
                run.bold = i == 0
            cell.paragraphs[0].paragraph_format.space_after = 0
            if i == 0:
                shade(cell, "E3EEEB")
    P("")


def figure(file, caption):
    doc.add_picture(str(HERE / "figures" / file), width=Inches(6.5))
    doc.paragraphs[-1].paragraph_format.keep_with_next = True
    note = P(caption)
    note.runs[0].italic = True
    note.runs[0].font.size = Pt(9.5)


def metric_row(horizon, label, m):
    passed = sum(m["passes"].values())
    return [HORIZONS[horizon], label, f"{m['mae']:.2f} ({m['mae_pct'] * 100:.2f}%)",
            f"{m['rmse_pct'] * 100:.2f}%", f"{m['mape'] * 100:.2f}%", f"{m['r2']:.3f}",
            f"{'Passed' if passed == 4 else 'Failed'} {passed}/4"]


METRIC_HEAD = ["Horizon", "Setup", "MAE in eggs (% of mean)", "RMSE %", "MAPE", "R²", "Result"]
METRIC_WIDTHS = [0.75, 1.6, 1.2, 0.7, 0.7, 0.6, 0.95]


def metrics_table(runs):
    table(METRIC_HEAD, [metric_row(h, label, T[h]["runs"][run]["metrics"])
                        for h in HORIZONS for run, label in runs], METRIC_WIDTHS)


doc.add_heading("Hybrid Gap Filling", 0)
P("ItikCare egg-yield forecasting. Change record, training parameters and test results.")
status = P("Status: tested in a dry run only. The ItikCare system and its database are unchanged. "
           "Applying this change needs adviser approval.")
status.runs[0].bold = True

H1("1. Summary")
bullets([
    "The panel asked for a daily dataset without gaps. Three of the four gaps (192 days) were filled with a hybrid method.",
    "The hybrid model passes all four forecasting thresholds for daily and tri-day yield.",
    "On the full test set it is slightly less accurate than the model trained on real data only.",
    "The simple rule \"same as yesterday\" is more accurate than both models.",
    "One gap stays empty because the flock was replaced during it.",
    "Open item: some egg counts in the source data form straight lines and should be checked against farm records (section 10).",
])
metrics_table([("real_level", "Real data only"), ("hybrid_level", "Hybrid fill")])

H1("2. What changed")
table(["Item", "Before (real data only)", "After (hybrid fill)"], [
    ["Days in the dataset", "666 observed", "858 (666 observed + 192 synthetic, 22.4%)"],
    ["Continuous segments", "5 caging periods", "2 series, one per flock generation"],
    ["Daily training / test rows", "554 / 97", "755 / 97"],
    ["Tri-day training / test rows", "544 / 97", "745 / 97"],
    ["Model inputs and target", "5 daily inputs, lag1 and roll3; egg yield", "Unchanged"],
], [1.8, 2.2, 2.5])
P("Training rows rise by 201: the 192 filled days, plus 9 real days at the start of periods 2, 4 and 5 "
  "that now have a 3-day history.")
table(["Gap", "Empty dates", "Days", "Action"], [
    ["1", "2024-06-18 to 2024-08-03", "47", "Filled"],
    ["2", "2024-10-29 to 2025-02-08", "103", "Left empty. The old flock was retired and a younger flock brought in."],
    ["3", "2025-07-09 to 2025-08-19", "42", "Filled"],
    ["4", "2025-10-30 to 2026-02-09", "103", "Filled"],
], [0.5, 1.9, 0.6, 3.5])

H1("3. The filled dataset")
figure("fig1_observed_and_filled.png",
       "Figure 1. Observed (black) and filled (orange) values. The blank stretch is gap 2. Where an orange "
       "line starts away from the last black point, it begins from the last training value instead of a test value.")
P("Filled values vary less than real ones, most clearly for humidity (standard deviation 7.4 observed, 2.9 filled). "
  "Filling makes the series continuous, but it does not recover the fluctuations that happened on those days.")

H1("4. Fill method")
table(["Columns", "Method", "How it works"], [
    ["Egg yield, flock size, flock age, feed intake", "Linear interpolation",
     "A straight line from the last usable day before the gap to the first real day after it. "
     "Egg yield, flock size and age are rounded to whole numbers."],
    ["Temperature, humidity", "Iterative imputer with Extra Trees",
     "Estimates each from the trend (days since the first record), the day of the year, and the other "
     "weather value. Rounded to one decimal."],
], [1.7, 1.4, 3.4])
H2("Imputer settings")
table(["Setting", "Value"], [
    ["Imputer", "scikit-learn IterativeImputer, max_iter=10, random_state=42"],
    ["Estimator", "ExtraTreesRegressor(n_estimators=100, random_state=42)"],
    ["Inputs", "trend, day_of_year, temperature_c, humidity_pct"],
    ["Other settings", "scikit-learn defaults (initial fill: mean)"],
    ["Iterations run", "10, the maximum. The imputer may not have fully converged."],
], [1.6, 4.9])
H2("Keeping test data out of the fill")
P("Data leakage happens when information from the test days reaches training. Here the test days are the "
  "last 15% of each caging period, right next to the gaps.")
bullets([
    "Split the real data into training and test days first.",
    "Start each straight line from the last training day's values, placed at the end of the period.",
    "Fit the weather imputer without any test-window day.",
    "Keep filled rows out of the test set. Score only on real days.",
], "List Number")
P("The line still ends at the first real day after the gap, which is later in time than the test days.")

H1("5. Training and parameters")
P("Both models use the existing pipeline in forecasting/pipeline.py. Daily and tri-day yield have separate models.")
table(["Step", "Detail"], [
    ["History features", "lag1 (yesterday's yield) and roll3 (mean of the previous 3 days), inside each continuous segment"],
    ["Split", "By date within each caging period: first 85% training, last 15% testing"],
    ["Estimator", "Median SimpleImputer, then StandardScaler, then RandomForestRegressor"],
    ["Tuning", "RandomizedSearchCV: 120 candidates, 4 time-ordered folds inside the training data, "
               "scored by mean absolute error, seed 42"],
], [1.6, 4.9])
H2("Parameters searched and chosen")
names = {"rf__n_estimators": "n_estimators (number of trees)", "rf__max_depth": "max_depth (tree depth)",
         "rf__min_samples_leaf": "min_samples_leaf (rows in a final leaf)",
         "rf__min_samples_split": "min_samples_split (rows needed to split)",
         "rf__max_features": "max_features (inputs tried per split)"}
space = R["settings"]["search_space"]
table(["Parameter", "Values searched", "Daily real", "Daily hybrid", "Tri-day real", "Tri-day hybrid"], [
    [label, ", ".join(str(v) for v in space[key])]
    + [str(T[h]["runs"][run]["best_params"][key]) for h in HORIZONS for run in ("real_level", "hybrid_level")]
    for key, label in names.items()
], [1.7, 1.6, 0.8, 0.8, 0.8, 0.8])

H1("6. Results")
P("All scores come from the same 97 real test days. Thresholds: MAE at most 8%, RMSE at most 10%, "
  "MAPE at most 15%, R² at least 0.75.")
metrics_table([("real_level", "Real data only"), ("hybrid_level", "Hybrid fill"),
               ("persistence", "Same as yesterday (no model)")])
figure("fig2_metrics_daily.png", "Figure 2. Daily yield metrics against the thresholds.")
figure("fig3_metrics_triday.png", "Figure 3. Tri-day yield metrics against the thresholds.")
figure("fig4_predictions_daily.png", "Figure 4. Daily yield: actual and predicted on test days.")
figure("fig5_predictions_triday.png", "Figure 5. Tri-day yield: actual and predicted on test days.")

H1("7. Feature importance")
figure("fig6_feature_importance.png", "Figure 6. Random Forest feature importance, real data only and hybrid fill.")
P("The recommendations use the daily model's importances only. In the daily model, yesterday's yield and the "
  "3-day average stay on top. Two lower pairs swap order: feed intake moves above flock size, and temperature "
  "moves just above humidity. The three prescriptive thresholds have not been re-tested.")
P("The tri-day importances change more (yesterday's yield 0.540 to 0.333, flock size 0.072 to 0.154, feed "
  "intake 0.037 to 0.135). The recommendations do not read them.")

H1("8. Decisions on the tri-day items")
table(["Item", "Effect", "Team decision"], [
    ["Tri-day feature importance shifts", "Not used by the recommendations. Tri-day still passes 4/4.",
     "Low priority. No change."],
    ["Tri-day overlap in the existing pipeline: the last 2 training rows of each period include egg counts "
     "from the first test days", "Measured below. Tri-day still passes 4/4 without those rows.",
     "Accepted. Not fixed; disclosed as a limitation."],
], [2.5, 2.5, 1.5])
H2("Measured effect of the overlap")
P(f"The overlapping training rows ({LEAK['real_level']['rows_removed']} in total) were removed and the "
  "tri-day models retrained.")
table(METRIC_HEAD, [
    row for run, label in (("real_level", "Real data"), ("hybrid_level", "Hybrid"))
    for row in (metric_row("triday", f"{label}, as is", T["triday"]["runs"][run]["metrics"]),
                metric_row("triday", f"{label}, overlap removed", LEAK[run]["metrics"]))
], METRIC_WIDTHS)
P("Without the overlap, error rises for both models, more for real data only. All thresholds are still met.")

H1("9. Other experiments")
figure("fig7_leakage.png", "Figure 7. Mean absolute error with and without leakage in the fill.")
P("When the straight lines started from the test days themselves, error appeared to drop. The drop disappeared "
  "once that leak was removed, so the leaked numbers are not valid.")
H2("Delta targeting (not adopted)")
P("Delta targeting predicts the change from yesterday and adds yesterday's value back. It lowers error, mostly "
  "toward the level of the \"same as yesterday\" rule, and changes feature importance heavily.")
metrics_table([("real_delta", "Real data, delta"), ("hybrid_delta", "Hybrid fill, delta")])

H1("10. Limitations and open items")
H2("Straight-line egg counts in the source data (open)")
P("In ItikCareDataSet.csv, 88 of 666 days sit in runs where egg yield changes by almost the same amount every "
  "day, while temperature and humidity still vary. This pattern usually comes from estimating or interpolating. "
  "The file alone cannot show whether these counts were recorded. Compare them with the farm's original records.")
table(["Dates", "Days", "Egg yield"], [
    ["2024-05-01 to 2024-06-02", "33", "198 to 129"], ["2024-06-02 to 2024-06-17", "16", "129 to 112"],
    ["2025-05-30 to 2025-06-30", "32", "283 to 220"], ["2025-07-01 to 2025-07-08", "8", "238 to 252"],
], [3, 1, 2])
P("42 of the 97 test days fall in these runs. Rescoring the same trained models on each group changes which "
  "model looks better. The models were not retrained without these days.")
table(["MAE in eggs", "Daily: straight (42)", "Daily: other (55)", "Tri-day: straight (42)", "Tri-day: other (55)"], [
    ["Real data only", "13.14", "11.75", "46.96", "41.56"],
    ["Hybrid fill", "15.44", "10.62", "56.44", "39.67"],
    ["Same as yesterday", "2.07", "9.00", "11.86", "23.55"],
], [1.7, 1.2, 1.2, 1.2, 1.2])
H2("Other limitations")
bullets([
    "192 of 858 days (22.4%) are estimates, not farm records. They must stay labelled and out of the DailyLog table.",
    "Filled weather was not compared with recorded weather for those dates.",
    "\"Same as yesterday\" (3 × yesterday for tri-day) passes all thresholds and beats both models.",
    "The tri-day overlap described in section 8 remains.",
    "Results come from one chronological split and one random seed.",
    "Seasonality was not tested.",
    "Project spec section 10 says gaps are expected and features must not span them. Spec section 10 and "
    "Chapter 3 need updating if this change is adopted.",
])

H1("11. For the manuscript and defense")
H2("Suggested Chapter 3 text")
P("Gaps from non-monitoring periods within a flock generation were filled using linear interpolation for egg "
  "yield, flock size, flock age and feed intake, and an iterative imputer with an Extra Trees estimator, using "
  "trend and day of year, for temperature and humidity. The gap in which the flock was replaced was not filled. "
  "Filled values are synthetic estimates, used only for training, and were computed without test-window values. "
  "Models were evaluated only on observed data. The hybrid model met all forecasting thresholds but did not "
  "improve accuracy on the full test set compared with observed data alone. For the tri-day model, a small number "
  "of training targets overlap the first test days; the thresholds were still met when those rows were removed.")
P("Settle the straight-line check in section 10 before using this text.")
H2("Likely questions")
for question, answer in (
    ("Is the filled data real?", "No. It is labelled synthetic and used only to make the series continuous for training."),
    ("Why is one gap still there?", "It separates two different flocks. A line between them would describe ducks that did not exist."),
    ("Did filling improve the model?", "Not on the full test set. It passes the thresholds and is slightly less accurate than real data alone."),
    ("Why use a model if yesterday's value is more accurate?", "Egg yield changes slowly, so yesterday is a strong "
     "predictor. The model also provides the feature importance that the recommendations use. The accuracy "
     "limitation remains and should be stated."),
):
    q = P(question)
    q.runs[0].bold = True
    q.paragraph_format.keep_with_next = True
    P(answer)

H1("12. Reproducing the results")
P("Run from the project root, in this order:")
for script in ("hybrid_fill_experiment", "check_triday_leak", "check_straight_runs", "build_report", "build_docx"):
    run = P("").add_run(f"venv/Scripts/python docs/hybrid_fill/{script}.py")
    run.font.name, run.font.size = "Consolas", Pt(9.5)
    doc.paragraphs[-1].paragraph_format.space_after = 0
V = R["versions"]
P(f"Built with scikit-learn {V['scikit-learn']}, pandas {V['pandas']}, NumPy {V['numpy']}. "
  "Source data: ItikCareDataSet.csv.")

# Page number in the footer.
footer = page.footer.paragraphs[0]
footer.alignment = 1
field = OxmlElement("w:fldSimple")
field.set(qn("w:instr"), "PAGE")
footer._p.append(field)

out = HERE / "Hybrid_Gap_Filling.docx"
doc.save(out)
print("saved", out)
