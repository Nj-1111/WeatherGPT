"""
M2 bias correction: MLP trained to predict GFS forecast error against ERA5
reanalysis. Runs on Kaggle 2xT4 or locally on CPU for smoke testing.

training/baseline_models.py runs LightGBM/ridge on the same splits and beats
this MLP by ~15% relative on held-out locations — check that before trusting
this model's output. Kept here as the resumable/multi-GPU reference
implementation, not as the recommended model.

Design notes:
  - nn.DataParallel wraps the model before .to(device), not after — wrapping
    a CPU-resident model makes DataParallel a no-op.
  - Checkpoints (model/optimizer/scheduler state + epoch + best_val_loss) save
    every --checkpoint-interval epochs and on improvement, locally and
    optionally to a Hugging Face repo. On restart the script resumes from the
    latest checkpoint instead of retraining from epoch 0.
  - Targets are standardized before the loss. Unscaled, temperature bias has
    ~6x the variance of precipitation bias, so a single MSE over both starves
    the precipitation head of gradient. Metrics are reported in real units
    (degC / mm) after inverting the scaling.
  - Two holdouts: a chronological tail (locations seen in training, later
    time) and a spatial holdout of entirely unseen locations. Only the
    spatial holdout reflects deployment, where a query names a village not
    among the training points.
  - A zero-correction baseline (trusting raw GFS) is computed on both
    holdouts so every RMSE has a do-nothing comparison to beat.
  - Reported metrics are from the best-val-loss epoch, matching the weights
    saved to best.pt, not the last epoch trained.

Ground truth is ERA5 reanalysis, not real station observations.

Usage:
    python kaggle_kernel_m2/train_m2.py --epochs 2 --checkpoint-interval 1 --no-hf
    python kaggle_kernel_m2/train_m2.py --epochs 30 --hf-repo-id you/weathergpt-m2-bias-correction
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pickle
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn


# People name this secret differently in the Kaggle UI, and a miss just silently
# disables uploads. Try the common names and say which one was found.
HF_TOKEN_SECRET_NAMES = ("HF_TOKEN", "HF_TOKEN_2", "HUGGINGFACE_TOKEN", "HF_API_TOKEN")


def get_hf_token(preferred: str | None = None) -> str | None:
    """Kaggle Secrets first (this script is meant to run there), env var fallback."""
    names = ([preferred] if preferred else []) + [n for n in HF_TOKEN_SECRET_NAMES if n != preferred]
    try:
        from kaggle_secrets import UserSecretsClient  # only importable on Kaggle
        client = UserSecretsClient()
        for name in names:
            try:
                token = client.get_secret(name)
            except Exception:
                continue  # secret not attached under this name; try the next
            if token:
                print(f"[hf] using Kaggle secret {name!r}")
                return token
    except Exception:
        pass
    for name in names:
        token = os.environ.get(name)
        if token:
            print(f"[hf] using environment variable {name!r}")
            return token
    print(f"[hf] no token found — looked for: {', '.join(names)} "
          f"(as Kaggle secrets and env vars). Pass --hf-token-secret NAME if yours differs.")
    return None


def get_git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, timeout=5
        ).decode().strip()
    except Exception:
        return os.environ.get("GIT_COMMIT", "unknown")


def find_data_file(explicit: str | None) -> Path:
    """Locate matched_pairs.csv without anyone needing to hardcode a Kaggle path.

    Kaggle mounts an attached dataset at /kaggle/input/<slug>/..., and the slug and
    nesting depth depend on how the dataset was created — so search recursively
    rather than guessing the layout.
    """
    # An explicit --data-path is a hard requirement, not a preference: silently
    # falling back to some other CSV would train on data the caller didn't ask for.
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise SystemExit(f"--data-path {explicit} does not exist.")
        return p

    candidates = [
        "training/datasets/matched_pairs.csv",
        "/kaggle/working/weathergpt/training/datasets/matched_pairs.csv",
    ]
    candidates += sorted(glob.glob("/kaggle/input/**/matched_pairs.csv", recursive=True))
    for c in candidates:
        p = Path(c)
        if p.exists():
            return p

    # Failure message lists what IS mounted, so the fix is obvious from the log alone.
    mounted = sorted(glob.glob("/kaggle/input/*"))
    hint = ""
    if mounted:
        csvs = sorted(glob.glob("/kaggle/input/**/*.csv", recursive=True))[:10]
        hint = ("\nMounted under /kaggle/input: " + ", ".join(Path(m).name for m in mounted))
        hint += ("\nCSV files found there: " + (", ".join(csvs) if csvs else "none")
                 + "\nIf your file is listed above under a different name, pass it with --data-path.")
    else:
        hint = "\nNothing is mounted under /kaggle/input — attach the dataset via 'Add Data' in the sidebar."
    raise SystemExit(
        "matched_pairs.csv not found. Checked: " + ", ".join(candidates) + hint
    )


class MLP(nn.Module):
    def __init__(self, in_dim=6, hidden=64, layers=3, dropout=0.1, out_dim=2):
        super().__init__()
        seq, d = [], in_dim
        for _ in range(layers):
            seq += [nn.Linear(d, hidden), nn.ReLU(), nn.Dropout(dropout)]
            d = hidden
        seq.append(nn.Linear(d, out_dim))
        self.net = nn.Sequential(*seq)

    def forward(self, x):
        return self.net(x)


FEATURE_COLS = ["gfs_t2m_k", "gfs_apcp_mm", "elevation_m", "lead_hours", "lat", "lon"]


def load_data(path: Path):
    import pandas as pd
    df = pd.read_csv(path)
    missing = [c for c in FEATURE_COLS + ["obs_t2m_c", "obs_apcp_mm"] if c not in df.columns]
    if missing:
        raise SystemExit(f"matched_pairs.csv is missing required columns: {missing}")

    # The chronological split below takes a *tail slice* and calls it an out-of-time
    # holdout. That is only true if the file is actually time-sorted. If someone
    # regenerates the CSV with a pipeline that doesn't sort, the "holdout" silently
    # degrades into a random tail and every reported number becomes optimistic —
    # so fail loudly rather than report a number that quietly means something else.
    if "valid_from" in df.columns:
        vf = pd.to_datetime(df["valid_from"], format="mixed", utc=True)
        if not vf.is_monotonic_increasing:
            raise SystemExit(
                f"{path} is not sorted ascending by valid_from. The chronological split "
                "would become a random tail and the reported holdout metrics would be "
                "invalid. Regenerate with training/build_matched_pairs.py (it sorts), "
                "or sort by valid_from before training."
            )
    else:
        print("[m2] WARNING: no valid_from column — cannot verify the CSV is time-sorted; "
              "the chronological split's out-of-time guarantee is unverified.")

    gfs_c = df["gfs_t2m_k"].values.astype(np.float32) - 273.15
    y_t = df["obs_t2m_c"].values.astype(np.float32) - gfs_c
    y_p = df["obs_apcp_mm"].values.astype(np.float32) - df["gfs_apcp_mm"].values.astype(np.float32)
    X = df[FEATURE_COLS].values.astype(np.float32)
    y = np.stack([y_t, y_p], axis=1).astype(np.float32)
    return X, y, df


def three_way_split(df, n_holdout_locations: int, val_frac: float, seed: int):
    """Split into train / temporal-val / spatial-holdout.

    Two different generalization questions, measured separately:
      - temporal val  : locations SEEN in training, a later time window. Answers
                        "can we correct bias at a known station going forward?"
      - spatial holdout: entire locations NEVER seen in training. Answers
                        "does this transfer to a village that isn't one of our
                        26 points?" — which is the actual deployment scenario.

    Reporting only the first (as the previous version did) overstates how well the
    model generalizes, because lat/lon/elevation are inputs and the model can simply
    memorize a per-location offset from ~960 rows per site.
    """
    location_id = df.groupby(["lat", "lon"]).ngroup().values
    unique_locations = np.unique(location_id)
    if n_holdout_locations >= len(unique_locations):
        raise SystemExit(
            f"--holdout-locations={n_holdout_locations} but the dataset only has "
            f"{len(unique_locations)} locations; leave at least a few for training."
        )

    spatial_mask = np.zeros(len(df), dtype=bool)
    if n_holdout_locations > 0:
        rng = np.random.default_rng(seed)
        held = rng.choice(unique_locations, size=n_holdout_locations, replace=False)
        spatial_mask = np.isin(location_id, held)

    # Rows are globally time-sorted (asserted in load_data), so filtering preserves
    # order and the tail of the in-sample rows is still a genuine later-time slice.
    in_sample_idx = np.where(~spatial_mask)[0]
    spatial_idx = np.where(spatial_mask)[0]
    split = int(len(in_sample_idx) * (1 - val_frac))
    return in_sample_idx[:split], in_sample_idx[split:], spatial_idx, location_id


def rmse(pred, true):
    return float(np.sqrt(np.mean((pred - true) ** 2)))


def save_checkpoint(path: Path, epoch, model, optimizer, scheduler, best_val_loss):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw_model = model.module if isinstance(model, nn.DataParallel) else model
    torch.save({
        "epoch": epoch,
        "model_state_dict": raw_model.state_dict(),
        "optimizer_state_dict": optimizer.state_dict(),
        "scheduler_state_dict": scheduler.state_dict() if scheduler else None,
        "best_val_loss": best_val_loss,
    }, path)


def load_checkpoint(path: Path, model, optimizer, scheduler, device):
    ckpt = torch.load(path, map_location=device)
    raw_model = model.module if isinstance(model, nn.DataParallel) else model
    raw_model.load_state_dict(ckpt["model_state_dict"])
    optimizer.load_state_dict(ckpt["optimizer_state_dict"])
    if scheduler and ckpt.get("scheduler_state_dict"):
        scheduler.load_state_dict(ckpt["scheduler_state_dict"])
    return ckpt["epoch"], ckpt["best_val_loss"]


class HFUploader:
    """Best-effort HF Hub upload — never raises out of training."""

    def __init__(self, repo_id: str | None, token: str | None):
        self.enabled = bool(repo_id and token)
        self.repo_id = repo_id
        if not self.enabled:
            print("[hf] disabled (no --hf-repo-id or no HF_TOKEN found) — checkpoints stay local only")
            return
        try:
            from huggingface_hub import HfApi
            self.api = HfApi(token=token)
            self.api.create_repo(repo_id=repo_id, repo_type="model", exist_ok=True)
            print(f"[hf] uploading to https://huggingface.co/{repo_id}")
        except Exception as exc:
            print(f"[hf] setup failed, disabling upload: {exc}")
            self.enabled = False

    def upload(self, local_path: Path, path_in_repo: str):
        if not self.enabled:
            return
        try:
            self.api.upload_file(
                path_or_fileobj=str(local_path),
                path_in_repo=path_in_repo,
                repo_id=self.repo_id,
                repo_type="model",
            )
            print(f"[hf] uploaded {path_in_repo}")
        except Exception as exc:
            print(f"[hf] upload of {path_in_repo} failed (continuing training): {exc}")

    def download_if_missing(self, local_path: Path, path_in_repo: str) -> bool:
        if local_path.exists() or not self.enabled:
            return local_path.exists()
        try:
            from huggingface_hub import hf_hub_download
            downloaded = hf_hub_download(repo_id=self.repo_id, filename=path_in_repo, repo_type="model")
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(Path(downloaded).read_bytes())
            print(f"[hf] pulled prior checkpoint {path_in_repo} for resume")
            return True
        except Exception as exc:
            print(f"[hf] no prior checkpoint found on hub ({exc}) — starting fresh")
            return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-path", default=None)
    ap.add_argument("--output-dir", default="training/models/bias_correction_v2")
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--batch-size", type=int, default=256)
    ap.add_argument("--lr", type=float, default=8e-4)
    ap.add_argument("--hidden-dim", type=int, default=128)
    ap.add_argument("--num-layers", type=int, default=3)
    ap.add_argument("--dropout", type=float, default=0.15)
    ap.add_argument("--checkpoint-interval", type=int, default=2)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--holdout-locations", type=int, default=4,
                    help="entire locations excluded from training, to measure transfer to unseen sites (0 disables)")
    ap.add_argument("--hf-repo-id", default=os.environ.get("HF_REPO_ID"))
    ap.add_argument("--hf-token-secret", default=None,
                    help="name of the Kaggle secret / env var holding the HF write token; "
                         "tried automatically: " + ", ".join(HF_TOKEN_SECRET_NAMES))
    ap.add_argument("--no-hf", action="store_true", help="disable HF upload even if a repo id/token is available")
    args = ap.parse_args()

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = out_dir / "checkpoint_latest.pt"
    metrics_history_path = out_dir / "metrics_history.jsonl"

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    n_gpus = torch.cuda.device_count() if torch.cuda.is_available() else 0
    print(f"[m2] device={device} gpus={n_gpus}")

    data_path = find_data_file(args.data_path)
    X, y, df = load_data(data_path)
    n_rows = len(df)
    tr_idx, va_idx, sp_idx, location_id = three_way_split(
        df, args.holdout_locations, args.val_frac, args.seed
    )
    n_locations = len(np.unique(location_id))
    print(f"[m2] loaded {n_rows} rows / {n_locations} locations from {data_path}")
    print(f"[m2] train {len(tr_idx)} | temporal-val {len(va_idx)} (seen locations, later time) "
          f"| spatial-holdout {len(sp_idx)} ({args.holdout_locations} unseen locations)")

    # The target is the bias residual, so "predict zero" == trusting raw GFS.
    # Computed on both holdouts so every model number has a do-nothing comparison.
    baseline = {
        "val_rmse_t": rmse(0.0, y[va_idx, 0]), "val_rmse_p": rmse(0.0, y[va_idx, 1]),
        "spatial_rmse_t": rmse(0.0, y[sp_idx, 0]) if len(sp_idx) else None,
        "spatial_rmse_p": rmse(0.0, y[sp_idx, 1]) if len(sp_idx) else None,
    }
    print(f"[m2] baseline (no correction)  val: rmse_t={baseline['val_rmse_t']:.4f} "
          f"rmse_p={baseline['val_rmse_p']:.4f}")
    if len(sp_idx):
        print(f"[m2] baseline (no correction)  spatial: rmse_t={baseline['spatial_rmse_t']:.4f} "
              f"rmse_p={baseline['spatial_rmse_p']:.4f}")

    from sklearn.preprocessing import StandardScaler
    scaler = StandardScaler().fit(X[tr_idx])
    X_scaled = scaler.transform(X).astype(np.float32)

    # Standardize targets too. Unscaled, one MSE over [temp_bias, precip_bias] is
    # dominated by temperature (~6:1 variance ratio here), starving the precipitation
    # head of gradient. Metrics are inverted back to real units before reporting.
    y_scaler = StandardScaler().fit(y[tr_idx])
    y_scaled = y_scaler.transform(y).astype(np.float32)
    print(f"[m2] target scaling: temp sigma={y_scaler.scale_[0]:.4f} degC, "
          f"precip sigma={y_scaler.scale_[1]:.4f} mm "
          f"(was a {(y_scaler.scale_[0] / y_scaler.scale_[1]) ** 2:.1f}:1 gradient imbalance)")

    with open(out_dir / "scaler.pkl", "wb") as f:
        pickle.dump(scaler, f)
    with open(out_dir / "target_scaler.pkl", "wb") as f:
        pickle.dump(y_scaler, f)

    Xt = torch.tensor(X_scaled[tr_idx])
    yt = torch.tensor(y_scaled[tr_idx])
    Xv = torch.tensor(X_scaled[va_idx]).to(device)
    yv_scaled = torch.tensor(y_scaled[va_idx]).to(device)
    Xs = torch.tensor(X_scaled[sp_idx]).to(device) if len(sp_idx) else None
    loader = torch.utils.data.DataLoader(
        torch.utils.data.TensorDataset(Xt, yt), batch_size=args.batch_size, shuffle=True
    )

    def evaluate(x_tensor, true_real):
        """RMSE in real units (degC / mm), undoing the target scaling."""
        pred = y_scaler.inverse_transform(model(x_tensor).cpu().numpy())
        return rmse(pred[:, 0], true_real[:, 0]), rmse(pred[:, 1], true_real[:, 1])

    model = MLP(in_dim=X.shape[1], hidden=args.hidden_dim, layers=args.num_layers, dropout=args.dropout)
    if n_gpus > 1:
        model = nn.DataParallel(model)
        print(f"[m2] wrapped in DataParallel across {n_gpus} GPUs")
    model = model.to(device)

    opt = torch.optim.AdamW(model.parameters(), lr=args.lr)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)
    loss_fn = nn.MSELoss()

    hf = HFUploader(None if args.no_hf else args.hf_repo_id, get_hf_token(args.hf_token_secret))
    hf.download_if_missing(ckpt_path, "checkpoint_latest.pt")

    run_config_path = out_dir / "run_config.json"
    run_config = {
        "epochs": args.epochs, "batch_size": args.batch_size, "lr": args.lr,
        "hidden_dim": args.hidden_dim, "num_layers": args.num_layers, "dropout": args.dropout,
        "seed": args.seed, "val_frac": args.val_frac, "holdout_locations": args.holdout_locations,
        "in_dim": X.shape[1], "feature_cols": FEATURE_COLS,
        "n_rows": n_rows, "n_locations": n_locations,
        "n_train": len(tr_idx), "n_val": len(va_idx), "n_spatial_holdout": len(sp_idx),
        "data_path": str(data_path), "git_commit": get_git_commit(),
        "baseline": baseline, "target_scaled": True,
        "ground_truth": "ERA5 reanalysis (not real station observations)",
    }

    start_epoch = 1
    best_val_loss = float("inf")
    if ckpt_path.exists():
        # CosineAnnealingLR was constructed with T_max=args.epochs, but the scheduler
        # state restored from the checkpoint encodes progress through the ORIGINAL
        # cycle. Resuming with a different --epochs silently produces a discontinuous,
        # wrong learning-rate curve rather than an error, so refuse it outright.
        if run_config_path.exists():
            prior = json.loads(run_config_path.read_text())
            if prior.get("epochs") != args.epochs:
                raise SystemExit(
                    f"Refusing to resume: this run was started with --epochs {prior.get('epochs')} "
                    f"but you passed --epochs {args.epochs}. The cosine LR schedule is planned across "
                    f"the full epoch count; changing it mid-run corrupts the schedule. Either rerun "
                    f"with --epochs {prior.get('epochs')}, or start fresh in a new --output-dir."
                )
            for key in ("holdout_locations", "val_frac", "seed"):
                if prior.get(key) != getattr(args, key.replace("-", "_")):
                    raise SystemExit(
                        f"Refusing to resume: --{key.replace('_', '-')} changed "
                        f"({prior.get(key)} -> {getattr(args, key)}). That changes the data split, so "
                        f"the checkpoint's weights and the new split are not comparable. "
                        f"Start fresh in a new --output-dir."
                    )
            run_config["git_commit"] = prior.get("git_commit", run_config["git_commit"])
        resumed_epoch, best_val_loss = load_checkpoint(ckpt_path, model, opt, scheduler, device)
        start_epoch = resumed_epoch + 1
        print(f"[m2] resumed from checkpoint at epoch {resumed_epoch}, continuing at epoch {start_epoch}")
    else:
        print("[m2] no checkpoint found, starting from epoch 1")

    if start_epoch > args.epochs:
        print(f"[m2] checkpoint epoch {start_epoch - 1} >= requested --epochs {args.epochs}; nothing to do")
        return

    with open(run_config_path, "w") as f:
        json.dump(run_config, f, indent=2)

    # Mirrors the epoch that produced best.pt — reporting last-epoch numbers next to
    # a best-epoch checkpoint would describe two different models.
    best = {"epoch": None, "val_rmse_t": None, "val_rmse_p": None,
            "spatial_rmse_t": None, "spatial_rmse_p": None}

    for epoch in range(start_epoch, args.epochs + 1):
        model.train()
        total_loss = 0.0
        for xb, yb in loader:
            xb, yb = xb.to(device), yb.to(device)
            opt.zero_grad()
            loss = loss_fn(model(xb), yb)
            loss.backward()
            opt.step()
            total_loss += loss.item() * len(xb)
        train_loss = total_loss / len(tr_idx)
        scheduler.step()

        model.eval()
        with torch.no_grad():
            val_loss = loss_fn(model(Xv), yv_scaled).item()
            val_rmse_t, val_rmse_p = evaluate(Xv, y[va_idx])
            sp_rmse_t, sp_rmse_p = evaluate(Xs, y[sp_idx]) if Xs is not None else (None, None)

        record = {
            "epoch": epoch, "train_loss": train_loss, "val_loss": val_loss,
            "val_rmse_t": val_rmse_t, "val_rmse_p": val_rmse_p,
            "spatial_rmse_t": sp_rmse_t, "spatial_rmse_p": sp_rmse_p,
            "lr": scheduler.get_last_lr()[0], "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with open(metrics_history_path, "a") as f:
            f.write(json.dumps(record) + "\n")
        spatial_str = f" | unseen-loc rmse_t {sp_rmse_t:.3f} rmse_p {sp_rmse_p:.3f}" if sp_rmse_t is not None else ""
        print(f"[m2] epoch {epoch:03d} train {train_loss:.4f} val {val_loss:.4f} "
              f"| val rmse_t {val_rmse_t:.3f} degC rmse_p {val_rmse_p:.3f} mm{spatial_str}")

        improved = val_loss < best_val_loss
        if improved:
            best_val_loss = val_loss
            best = {"epoch": epoch, "val_rmse_t": val_rmse_t, "val_rmse_p": val_rmse_p,
                    "spatial_rmse_t": sp_rmse_t, "spatial_rmse_p": sp_rmse_p}
        due_for_checkpoint = (epoch % args.checkpoint_interval == 0) or improved or epoch == args.epochs
        if due_for_checkpoint:
            save_checkpoint(ckpt_path, epoch, model, opt, scheduler, best_val_loss)
            hf.upload(ckpt_path, "checkpoint_latest.pt")
            if improved:
                best_path = out_dir / "best.pt"
                raw_model = model.module if isinstance(model, nn.DataParallel) else model
                torch.save(raw_model.state_dict(), best_path)
                hf.upload(best_path, "best.pt")

    plot_loss_curves(metrics_history_path, out_dir)
    hf.upload(out_dir / "loss_curves.png", "loss_curves.png")

    with open(out_dir / "config.json", "w") as f:
        json.dump({"in_dim": X.shape[1], "out_dim": 2, "hidden": args.hidden_dim, "layers": args.num_layers}, f, indent=2)

    def vs_baseline(model_val, base_val):
        """Signed % change vs doing nothing. Positive = better than raw GFS,
        negative = the correction actively made things worse."""
        if model_val is None or not base_val:
            return None
        return 100 * (1 - model_val / base_val)

    final_metrics = {
        "best_epoch": best["epoch"], "best_val_loss": best_val_loss,
        "val_rmse_t": best["val_rmse_t"], "val_rmse_p": best["val_rmse_p"],
        "spatial_rmse_t": best["spatial_rmse_t"], "spatial_rmse_p": best["spatial_rmse_p"],
        "baseline": baseline,
        "val_rmse_t_pct_vs_baseline": vs_baseline(best["val_rmse_t"], baseline["val_rmse_t"]),
        "val_rmse_p_pct_vs_baseline": vs_baseline(best["val_rmse_p"], baseline["val_rmse_p"]),
        "spatial_rmse_t_pct_vs_baseline": vs_baseline(best["spatial_rmse_t"], baseline["spatial_rmse_t"]),
        "spatial_rmse_p_pct_vs_baseline": vs_baseline(best["spatial_rmse_p"], baseline["spatial_rmse_p"]),
        "epochs_run": args.epochs, "dataset_kind": "real_matched_pairs",
        "split": f"chronological_tail_{int(args.val_frac * 100)}pct + {args.holdout_locations}_location_spatial_holdout",
        "metrics_are_from": "best-val-loss epoch (same weights as best.pt)",
        "units": {"rmse_t": "degC", "rmse_p": "mm"},
        "n_rows": n_rows, "n_locations": n_locations, "git_commit": run_config["git_commit"],
    }
    with open(out_dir / "metrics.json", "w") as f:
        json.dump(final_metrics, f, indent=2)
    for name in ("config.json", "metrics.json", "run_config.json", "scaler.pkl",
                 "target_scaler.pkl", "metrics_history.jsonl"):
        hf.upload(out_dir / name, name)

    def fmt(model_val, base_val, unit):
        pct = vs_baseline(model_val, base_val)
        if pct is None:
            return "n/a"
        verdict = "better than" if pct >= 0 else "WORSE than"
        return f"{model_val:.3f}{unit} (baseline {base_val:.3f}{unit}, {abs(pct):.1f}% {verdict} no correction)"

    print(f"\n[m2] done. best epoch {best['epoch']} (val_loss {best_val_loss:.4f})")
    print(f"[m2]   seen locations, later time : temp {fmt(best['val_rmse_t'], baseline['val_rmse_t'], 'degC')}")
    print(f"[m2]                              : rain {fmt(best['val_rmse_p'], baseline['val_rmse_p'], 'mm')}")
    if best["spatial_rmse_t"] is not None:
        print(f"[m2]   UNSEEN locations           : temp {fmt(best['spatial_rmse_t'], baseline['spatial_rmse_t'], 'degC')}")
        print(f"[m2]                              : rain {fmt(best['spatial_rmse_p'], baseline['spatial_rmse_p'], 'mm')}")
        print("[m2]   ^ the unseen-location row is the one that reflects real deployment.")


def plot_loss_curves(metrics_history_path: Path, out_dir: Path):
    if not metrics_history_path.exists():
        return
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[m2] matplotlib not installed, skipping loss curve plot")
        return

    with open(metrics_history_path) as f:
        records = [json.loads(line) for line in f if line.strip()]
    if not records:
        return
    epochs = [r["epoch"] for r in records]
    has_spatial = records[0].get("spatial_rmse_t") is not None

    fig, axes = plt.subplots(1, 3, figsize=(15, 4))
    axes[0].plot(epochs, [r["train_loss"] for r in records], label="train")
    axes[0].plot(epochs, [r["val_loss"] for r in records], label="val")
    axes[0].set_title("Loss (standardized targets)"); axes[0].set_xlabel("epoch"); axes[0].legend()

    # Plotting seen-location and unseen-location error on the same axes makes the
    # generalization gap visible directly — a widening gap means the model is
    # learning per-location offsets rather than transferable bias structure.
    axes[1].plot(epochs, [r["val_rmse_t"] for r in records], label="seen locations")
    if has_spatial:
        axes[1].plot(epochs, [r["spatial_rmse_t"] for r in records], label="unseen locations", linestyle="--")
    axes[1].set_title("Temperature RMSE (deg C)"); axes[1].set_xlabel("epoch"); axes[1].legend()

    axes[2].plot(epochs, [r["val_rmse_p"] for r in records], label="seen locations")
    if has_spatial:
        axes[2].plot(epochs, [r["spatial_rmse_p"] for r in records], label="unseen locations", linestyle="--")
    axes[2].set_title("Precipitation RMSE (mm)"); axes[2].set_xlabel("epoch"); axes[2].legend()

    fig.tight_layout()
    fig.savefig(out_dir / "loss_curves.png", dpi=120)
    plt.close(fig)
    print(f"[m2] wrote {out_dir / 'loss_curves.png'}")


if __name__ == "__main__":
    main()
