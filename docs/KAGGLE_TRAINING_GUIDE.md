# Running M2 training on Kaggle (2×T4, resumable, Hugging Face checkpoints)

This is a manual, browser-driven workflow — you run this yourself on kaggle.com. `kaggle_kernel_m2/train_m2.py` is the script; this doc is the exact click-by-click sequence.

Verified locally before this guide was written: the script ran a real 6-epoch CPU smoke test on the actual 24,960-row dataset (`training/datasets/matched_pairs.csv`), correctly resumed mid-run after being killed, and correctly disabled Hugging Face upload without crashing when given a bad token. See `docs/M2_TRAINING_REPORT.md` for those results once populated with a real Kaggle run.

## 1. Get the dataset onto Kaggle

`training/datasets/matched_pairs.csv` is `.gitignore`d (not in the repo you'd push), so it needs to go to Kaggle as a **Kaggle Dataset**, not via the code:

1. Go to kaggle.com → **Datasets** → **New Dataset**.
2. Upload your local `training/datasets/matched_pairs.csv` (1.6MB — instant).
3. Title it something like `weathergpt-matched-pairs`, keep it **Private**, click **Create**.
4. Note the dataset's URL slug (e.g. `your-username/weathergpt-matched-pairs`) — you'll attach it to the notebook in step 3.

## 2. Create a Hugging Face model repo (if you don't have one yet)

1. On huggingface.co, go to **New Model**, name it (e.g. `weathergpt-m2-bias-correction`), keep it private if you want.
2. Go to **Settings → Access Tokens**, create a token with **write** access. Copy it — you won't see it again.

## 3. Create the Kaggle notebook

1. kaggle.com → **Code** → **New Notebook**.
2. **Settings** (right sidebar) → **Accelerator** → select **GPU T4 x2**. This is the step that actually gives you 2 GPUs — `kaggle_kernel_official/kernel-metadata.json` for the *other* existing kernel in this repo has `"enable_gpu": false`, which is why that one never got a GPU despite its "T4 x2" framing. Don't repeat that mistake here.
3. **Settings** → **Internet** → **On** (needed for the Hugging Face upload).
4. **Add Data** (right sidebar) → search for the dataset you created in step 1 → **Add**. It'll be mounted at `/kaggle/input/weathergpt-matched-pairs/matched_pairs.csv` (or similar — the script already globs `/kaggle/input/*/matched_pairs.csv` automatically, so you don't need to hardcode the exact path).

## 4. Add your Hugging Face token as a Kaggle Secret

1. In the notebook, **Add-ons** (top menu) → **Secrets**.
2. **Add a new secret**: Value = the token from step 2. Label it any of
   `HF_TOKEN`, `HF_TOKEN_2`, `HUGGINGFACE_TOKEN`, or `HF_API_TOKEN` — the script tries
   all four and prints which one it found. For any other name, pass
   `--hf-token-secret YOUR_NAME`.
3. Make sure it's **attached/enabled** for this specific notebook (Kaggle secrets are opt-in per-notebook).

You'll see `[hf] using Kaggle secret 'HF_TOKEN_2'` (or whichever) in the log. If the token
is missing the script prints exactly what it looked for and continues training with uploads
disabled — it never dies mid-run over a token.

## 5. Install dependencies

In the first notebook cell:

```bash
!pip install -q huggingface_hub
```

That's the only extra package — `torch`, `numpy`, `pandas`, `scikit-learn`, `matplotlib` are all already present in Kaggle's default Python environment.

## 6. Get the script into the notebook

**Preferred — upload it as a file.** `File → Upload` → pick `kaggle_kernel_m2/train_m2.py`.
This avoids the failure mode where pasting into a `%%writefile` cell drops a stray character
into the source (a stray path pasted mid-file produced
`NameError: name 'kaggle' is not defined` on a real run — the script was fine, the paste wasn't).

If you do use `%%writefile train_m2.py`, paste the file **unmodified**. You should not need to
edit any path: `find_data_file()` searches `/kaggle/input/**` recursively and will locate
`matched_pairs.csv` wherever Kaggle mounted it. If it can't, the error lists every CSV it
*did* find under `/kaggle/input`, so you can pass the right one with `--data-path`.

Then in the next cell:

```python
!python train_m2.py --epochs 30 --hf-repo-id your-username/weathergpt-m2-bias-correction
```

(Replace `your-username/weathergpt-m2-bias-correction` with the exact repo you made in step 2.)

If you'd rather run it as a proper `.py` file instead of inline: **File → Upload** the script as `train_m2.py` into the notebook's working directory, then run the same command above in a code cell.

## 7. What to watch for while it runs

Expected output, in order:

```
[m2] device=cuda gpus=2                              <- confirms both T4s were detected
[m2] loaded 24960 rows from ... -> train 21216 / val 3744 (chronological tail)
[m2] baseline (no correction) rmse_t=2.13 rmse_p=0.79  <- sanity-check number, should be non-zero
[hf] uploading to https://huggingface.co/your-username/weathergpt-m2-bias-correction
[m2] no checkpoint found, starting from epoch 1
[m2] epoch 001 train ... val ... rmse_t ... rmse_p ... brier ...
[hf] uploaded checkpoint_latest.pt                    <- confirms the HF push is actually working
...
```

**If `device=cpu gpus=0` appears instead of `device=cuda gpus=2`**: the GPU accelerator wasn't actually enabled — go back to step 3.1 and check the setting stuck (Kaggle sometimes needs the notebook re-saved/restarted after changing the accelerator).

**If you see `[hf] setup failed, disabling upload: ...401...`**: the secret isn't attached correctly, or the token doesn't have write access — training will still complete fine locally on Kaggle's disk, you just won't get remote checkpoints. Fix the secret and re-run.

## 8. If the Kaggle session times out mid-run (12-hour limit)

This is exactly what the resumable-checkpoint design is for. When your session gets killed:

1. Open a **new** Kaggle session (or re-run the notebook).
2. Re-attach the same dataset and the same `HF_TOKEN` secret (steps 3–4 again).
3. Run the **exact same command** as before — same `--epochs 30`, same `--hf-repo-id`:
   ```python
   !python train_m2.py --epochs 30 --hf-repo-id your-username/weathergpt-m2-bias-correction
   ```
4. The script pulls the last checkpoint from your Hugging Face repo automatically (`hf_hub_download`) if it's not already sitting locally, and prints `[m2] resumed from checkpoint at epoch N, continuing at epoch N+1` — it does **not** restart from epoch 1.

**Important**: always resume with the **same `--epochs` value** as the original run. The learning-rate schedule (`CosineAnnealingLR`) is planned across the full epoch count; changing it between the interrupted run and the resume produces a discontinuous, worse learning-rate curve. (Verified this exact failure mode locally — changing `--epochs` mid-resume works and doesn't crash, but the LR schedule becomes inconsistent. Keep the epoch count fixed across a resume.)

## 9. When it's done

Final console output looks like:

```
[m2] done. best_val_loss=1.23 rmse_t 1.10 (baseline 2.13, 48.4% better) rmse_p 0.55 (baseline 0.79, 30.4% better)
```

The `X% better` numbers are the actual benchmark — how much the model improves over just trusting raw GFS with no correction at all. That's the number worth reporting, not the raw RMSE alone.

Everything you need for `docs/M2_TRAINING_REPORT.md` will be sitting in:
- Locally on Kaggle: `training/models/bias_correction_v2/{metrics.json, run_config.json, metrics_history.jsonl, loss_curves.png}`
- On Hugging Face: `checkpoint_latest.pt`, `best.pt`, and copies of all the above files, at `https://huggingface.co/your-username/weathergpt-m2-bias-correction`

Copy `metrics.json`'s contents (or just paste the final console line) back to me and I'll write up `docs/M2_TRAINING_REPORT.md` and update `docs/PROOF_OF_WORK.md` with it.
