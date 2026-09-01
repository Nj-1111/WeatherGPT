"""
Non-neural baselines for M2 bias correction, on the EXACT same splits the MLP uses.

Why this exists: at ~25k rows and 6 low-dimensional heterogeneous features, the
published evidence says gradient-boosted trees usually beat neural nets on tabular
data (Grinsztajn et al., NeurIPS 2022; Shwartz-Ziv & Armon, 2021). Before spending
GPU hours tuning kaggle_kernel_m2/train_m2.py, this establishes whether the MLP is
actually earning its complexity — and gives a "predict the training mean" floor so
we can tell a real model apart from one that has only learned the average bias.

Runs on CPU in seconds. Reuses train_m2.load_data / three_way_split directly so the
train / temporal-val / spatial-holdout rows are byte-identical to the MLP's.

Usage:
    python training/baseline_models.py
    python training/baseline_models.py --holdout-locations 4 --seed 42
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

_spec = importlib.util.spec_from_file_location(
    "train_m2", Path(__file__).resolve().parent.parent / "kaggle_kernel_m2" / "train_m2.py"
)
_m2 = importlib.util.module_from_spec(_spec)
sys.modules["train_m2"] = _m2
_spec.loader.exec_module(_m2)

TARGETS = [("temperature", 0, "degC"), ("precipitation", 1, "mm")]


def evaluate(name, pred_val, pred_sp, y, va_idx, sp_idx, results):
    row = {"model": name}
    for label, col, unit in TARGETS:
        row[f"val_rmse_{label}"] = _m2.rmse(pred_val[:, col], y[va_idx, col])
        row[f"spatial_rmse_{label}"] = (
            _m2.rmse(pred_sp[:, col], y[sp_idx, col]) if len(sp_idx) else None
        )
    results.append(row)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-path", default=None)
    ap.add_argument("--holdout-locations", type=int, default=4)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--output", default="training/models/baseline_comparison.json")
    args = ap.parse_args()

    data_path = _m2.find_data_file(args.data_path)
    X, y, df = _m2.load_data(data_path)
    tr_idx, va_idx, sp_idx, location_id = _m2.three_way_split(
        df, args.holdout_locations, args.val_frac, args.seed
    )
    print(f"[baseline] {len(df)} rows / {len(np.unique(location_id))} locations")
    print(f"[baseline] train {len(tr_idx)} | temporal-val {len(va_idx)} | spatial-holdout {len(sp_idx)}\n")

    results = []

    # ---- 0. do nothing (trust raw GFS). The number every model must beat. ----
    zeros_v, zeros_s = np.zeros((len(va_idx), 2)), np.zeros((len(sp_idx), 2))
    evaluate("no correction (raw GFS)", zeros_v, zeros_s, y, va_idx, sp_idx, results)

    # ---- 1. predict the training-set mean bias. A constant offset, no features. ----
    # If a model can't clearly beat this, it has learned nothing from the inputs.
    mean_bias = y[tr_idx].mean(axis=0)
    evaluate("constant (train mean bias)",
             np.tile(mean_bias, (len(va_idx), 1)), np.tile(mean_bias, (len(sp_idx), 1)),
             y, va_idx, sp_idx, results)

    # ---- 2. ridge regression. Linear floor. ----
    from sklearn.linear_model import Ridge
    from sklearn.preprocessing import StandardScaler
    sc = StandardScaler().fit(X[tr_idx])
    ridge = Ridge(alpha=1.0).fit(sc.transform(X[tr_idx]), y[tr_idx])
    evaluate("ridge (linear)",
             ridge.predict(sc.transform(X[va_idx])), ridge.predict(sc.transform(X[sp_idx])),
             y, va_idx, sp_idx, results)

    # ---- 3. LightGBM, one model per target (they have very different distributions:
    #         temp bias is roughly continuous, precip bias is ~77% exact zeros). ----
    import lightgbm as lgb
    pred_v = np.zeros((len(va_idx), 2))
    pred_s = np.zeros((len(sp_idx), 2))
    importances = {}
    for label, col, unit in TARGETS:
        model = lgb.LGBMRegressor(
            n_estimators=600, learning_rate=0.05, num_leaves=31,
            min_child_samples=40, subsample=0.9, subsample_freq=1,
            colsample_bytree=0.9, reg_lambda=1.0, random_state=args.seed, verbose=-1,
        )
        model.fit(
            X[tr_idx], y[tr_idx, col],
            eval_set=[(X[va_idx], y[va_idx, col])],
            eval_metric="rmse",
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        pred_v[:, col] = model.predict(X[va_idx])
        if len(sp_idx):
            pred_s[:, col] = model.predict(X[sp_idx])
        importances[label] = dict(zip(_m2.FEATURE_COLS,
                                      (model.feature_importances_ / model.feature_importances_.sum()).round(4).tolist()))
        print(f"[baseline] lgbm/{label}: stopped at {model.best_iteration_} trees")
    evaluate("lightgbm", pred_v, pred_s, y, va_idx, sp_idx, results)

    # ---- report ----
    base = results[0]
    print(f"\n{'model':<28} {'val temp':>12} {'val rain':>12} {'UNSEEN temp':>14} {'UNSEEN rain':>14}")
    print("-" * 84)
    for r in results:
        def cell(key):
            v = r[key]
            if v is None:
                return f"{'n/a':>12}"
            if r["model"] == base["model"]:
                return f"{v:>12.3f}"
            pct = 100 * (1 - v / base[key])
            return f"{v:>7.3f}({pct:+.0f}%)"
        print(f"{r['model']:<28} {cell('val_rmse_temperature'):>12} {cell('val_rmse_precipitation'):>12} "
              f"{cell('spatial_rmse_temperature'):>14} {cell('spatial_rmse_precipitation'):>14}")
    print("\n(% = improvement vs. doing nothing; UNSEEN = locations never in training,")
    print(" which is the column that reflects real deployment.)")

    print("\n[baseline] LightGBM feature importance (fraction of splits):")
    for label, imps in importances.items():
        ranked = sorted(imps.items(), key=lambda kv: -kv[1])
        print(f"  {label:<14} " + "  ".join(f"{k}={v:.2f}" for k, v in ranked))

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump({
            "data_path": str(data_path), "n_rows": len(df),
            "n_train": len(tr_idx), "n_val": len(va_idx), "n_spatial_holdout": len(sp_idx),
            "holdout_locations": args.holdout_locations, "seed": args.seed,
            "units": {"temperature": "degC", "precipitation": "mm"},
            "ground_truth": "ERA5 reanalysis (not real station observations)",
            "results": results, "lgbm_feature_importance": importances,
        }, f, indent=2)
    print(f"\n[baseline] wrote {out}")


if __name__ == "__main__":
    main()
