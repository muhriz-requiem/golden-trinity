"""
train_lstm_v4.py — GoldenTrinity LSTM Training (Fixed + Checkpointing)
=======================================================
Root cause of v3/overnight failure: Sharpe 0.0 / no trades fired.

Fix strategy:
1. Dynamic threshold using percentile of actual returns
2. Loose sim gate (0.38/0.38) so trades fire during early training
3. Checkpoint after every fold — safe to kill and resume anytime

SAFE SHUTDOWN: Just close the window or press Ctrl+C.
The script saves after every completed fold.
Next run automatically skips completed configs/folds.

RESUME: Just run the script again. It reads the checkpoint file
and picks up from where it left off.
"""
import os, json, time
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, WeightedRandomSampler
from datetime import datetime
import requests

DATA_1MIN      = r"D:\golden-trinity\data\GLD_1min.csv"
DATA_5MIN      = r"D:\golden-trinity\data\GLD_5min.csv"
MODEL_DIR      = r"D:\golden-trinity\model"
MODEL_FILE     = os.path.join(MODEL_DIR, "lstm_model.pt")
METRICS_FILE   = os.path.join(MODEL_DIR, "training_metrics_v4.json")
CHECKPOINT_FILE = os.path.join(MODEL_DIR, "training_checkpoint.json")

DISCORD_REPORTS = "<discord-webhook-reports>"
DISCORD_HEALTH  = "<discord-webhook-health>"

os.makedirs(MODEL_DIR, exist_ok=True)

HIDDEN_SIZE  = 64
NUM_LAYERS   = 2
DROPOUT      = 0.2
BATCH_SIZE   = 512
LR           = 0.001
SIM_HIGH     = 0.38
SIM_LOW      = 0.38

CONFIGS = [
    {"name": "v4_A_5min_p40",  "data": "5min", "forward": 6,  "window": 60,  "hidden": 64,  "percentile": 40, "epochs": 80},
    {"name": "v4_B_5min_p35",  "data": "5min", "forward": 6,  "window": 60,  "hidden": 64,  "percentile": 35, "epochs": 80},
    {"name": "v4_C_5min_deep", "data": "5min", "forward": 6,  "window": 60,  "hidden": 128, "percentile": 40, "epochs": 60},
    {"name": "v4_D_1min_p40",  "data": "1min", "forward": 15, "window": 60,  "hidden": 64,  "percentile": 40, "epochs": 60},
    {"name": "v4_E_1min_deep", "data": "1min", "forward": 15, "window": 60,  "hidden": 128, "percentile": 40, "epochs": 60},
    {"name": "v4_F_5min_long", "data": "5min", "forward": 12, "window": 120, "hidden": 64,  "percentile": 40, "epochs": 60},
]


# ── Checkpoint helpers ─────────────────────────────────────────────────────

def load_checkpoint() -> dict:
    """Load saved progress. Returns empty dict if no checkpoint exists."""
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE) as f:
                ckpt = json.load(f)
            print(f"[checkpoint] Resuming from checkpoint — "
                  f"{len(ckpt.get('completed_configs', []))} configs already done")
            return ckpt
        except Exception:
            pass
    return {
        "completed_configs": [],   # list of config names fully done
        "all_results":       [],   # results so far
        "global_best_sh":    -999.0,
        "global_best_cfg":   None,
        "global_best_hid":   64,
        "started_at":        datetime.now().isoformat()
    }

def save_checkpoint(ckpt: dict):
    """Save progress to disk. Called after every completed fold."""
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(ckpt, f, indent=2)

def clear_checkpoint():
    """Delete checkpoint after successful full run."""
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        print("[checkpoint] Cleared — training complete")


# ── Discord ────────────────────────────────────────────────────────────────

def post_discord(webhook, msg):
    try:
        requests.post(webhook, json={"content": msg}, timeout=5)
    except Exception:
        pass


# ── Feature engineering ────────────────────────────────────────────────────

def ema(arr, p):
    k = 2 / (p + 1)
    out = np.zeros(len(arr))
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = arr[i] * k + out[i-1] * (1 - k)
    return out


def session_enc(h):
    if 0 <= h < 7:     return [1, 0, 0, 0]
    elif 7 <= h < 13:  return [0, 1, 0, 0]
    elif 13 <= h < 17: return [0, 0, 1, 0]
    elif 17 <= h < 21: return [0, 0, 0, 1]
    return [0, 0, 0, 0]


def compute_dynamic_threshold(closes, forward_bars, percentile):
    rets = [abs((closes[i+forward_bars]-closes[i])/closes[i])
            for i in range(len(closes)-forward_bars)]
    thr = float(np.percentile(rets, percentile))
    print(f"  Dynamic threshold (p{percentile}): {thr:.5f} = {thr*100:.3f}%")
    return thr


def make_label(closes, i, fwd, thr):
    if i + fwd >= len(closes):
        return 1
    r = (closes[i+fwd] - closes[i]) / closes[i]
    return 0 if r > thr else (2 if r < -thr else 1)


def prepare(df, s, e, window, fwd, thr):
    closes = df["close"].values
    vols   = df["volume"].values
    hours  = df.index.hour
    e9     = ema(closes, 9)
    e21    = ema(closes, 21)
    rm     = pd.Series(closes).rolling(20).mean().values
    rs     = pd.Series(closes).rolling(20).std().values
    X, Xe, Y = [], [], []
    for i in range(max(s, window + 21), e - fwd):
        wc  = closes[i-window:i]
        wv  = vols[i-window:i]
        ed  = (e9[i-window:i] - e21[i-window:i]) / (closes[i] + 1e-8)
        std = rs[i] if rs[i] > 0 else 1e-8
        bb  = (wc - rm[i]) / std
        np_ = (wc - wc.mean()) / (wc.std() + 1e-8)
        nv  = (wv - wv.mean()) / (wv.std() + 1e-8)
        Xe.append([0.0] + session_enc(int(hours[i])) + [1.0])
        X.append(np.stack([np_, nv, ed, bb], axis=-1))
        Y.append(make_label(closes, i, fwd, thr))
    return (
        np.array(X,  dtype=np.float32),
        np.array(Xe, dtype=np.float32),
        np.array(Y,  dtype=np.int64)
    )


# ── Model ──────────────────────────────────────────────────────────────────

class LSTMModel(nn.Module):
    def __init__(self, hidden=64):
        super().__init__()
        self.lstm = nn.LSTM(input_size=4, hidden_size=hidden,
                            num_layers=NUM_LAYERS, batch_first=True, dropout=DROPOUT)
        self.attn = nn.Linear(hidden, 1)
        self.fc   = nn.Linear(hidden + 6, 3)
        self.soft = nn.Softmax(dim=-1)

    def forward(self, x, extra):
        out, _ = self.lstm(x)
        a   = torch.softmax(self.attn(out), dim=1)
        ctx = (a * out).sum(dim=1)
        return self.soft(self.fc(torch.cat([ctx, extra], dim=-1)))


class DS(Dataset):
    def __init__(self, X, e, y):
        self.X = torch.tensor(X,  dtype=torch.float32)
        self.e = torch.tensor(e,  dtype=torch.float32)
        self.y = torch.tensor(y,  dtype=torch.long)
    def __len__(self):        return len(self.y)
    def __getitem__(self, i): return self.X[i], self.e[i], self.y[i]


def compute_sharpe(rets, bars_per_year=252*78):
    r = np.array(rets)
    if len(r) < 5 or r.std() == 0:
        return 0.0
    return float(r.mean() / r.std() * np.sqrt(bars_per_year))


def sim_returns(model, loader, device):
    model.eval()
    rets = []
    correct = total = 0
    with torch.no_grad():
        for x, e, y in loader:
            p   = model(x.to(device), e.to(device))
            pu  = p[:, 0].cpu().numpy()
            pd_ = p[:, 2].cpu().numpy()
            yt  = y.numpy()
            for i in range(len(pu)):
                if pu[i] >= SIM_HIGH and pd_[i] <= SIM_LOW:
                    hit = yt[i] == 0
                    rets.append(0.0015 if hit else -0.0007)
                    correct += int(hit); total += 1
                elif pd_[i] >= SIM_HIGH and pu[i] <= SIM_LOW:
                    hit = yt[i] == 2
                    rets.append(0.0015 if hit else -0.0007)
                    correct += int(hit); total += 1
    acc = correct / total if total > 0 else 0
    return rets, acc, total


# ── Diagnosis ──────────────────────────────────────────────────────────────

def diagnose_data(dfs):
    print("\n" + "="*60)
    print("DATA DIAGNOSIS")
    print("="*60)
    for name, df in dfs.items():
        closes = df["close"].values
        fwd = 6 if name == "5min" else 15
        print(f"\n{name}: {len(df)} bars | ${closes[0]:.2f} to ${closes[-1]:.2f}")
        rets = np.array([(closes[i+fwd]-closes[i])/closes[i]
                         for i in range(len(closes)-fwd)])
        print(f"  fwd-{fwd} std={rets.std()*100:.3f}%  "
              f"p40_abs={np.percentile(abs(rets),40)*100:.3f}%  "
              f"p50_abs={np.percentile(abs(rets),50)*100:.3f}%")
        for thr in [0.001, 0.002, 0.003, 0.004, 0.005]:
            up = (rets > thr).mean(); dn = (rets < -thr).mean()
            print(f"    thr={thr:.3f}: UP={up:.1%}  FLAT={(1-up-dn):.1%}  DOWN={dn:.1%}")
    print()


# ── Training ───────────────────────────────────────────────────────────────

def run_config(cfg, dfs, device, ckpt):
    """
    Run one config with 3-fold walk-forward.
    Saves checkpoint after each fold — safe to interrupt between folds.
    """
    df     = dfs[cfg["data"]]
    window = cfg["window"]
    fwd    = cfg["forward"]
    hidden = cfg["hidden"]
    epochs = cfg["epochs"]

    n  = len(df)
    fs = n // 4

    thr = compute_dynamic_threshold(df["close"].values, fwd, cfg["percentile"])

    # Check if we have partial fold results saved for this config
    partial_key  = f"partial_{cfg['name']}"
    saved_folds  = ckpt.get(partial_key, {}).get("fold_sharpes", [])
    start_fold   = len(saved_folds)
    best_sh      = ckpt.get(partial_key, {}).get("best_sh", -999.0)

    if start_fold > 0:
        print(f"  Resuming from fold {start_fold+1} "
              f"(folds {list(range(1, start_fold+1))} already done)")

    fold_sharpes = list(saved_folds)
    best_st      = None  # can't resume model weights mid-fold, only between folds

    for fold in range(start_fold, 3):
        te = fs * (fold + 1)
        ve = min(te + fs, n)

        tX, tXe, tY = prepare(df, 0,  te, window, fwd, thr)
        vX, vXe, vY = prepare(df, te, ve, window, fwd, thr)

        if len(tY) < 500:
            print(f"  Fold {fold+1}: only {len(tY)} samples, skipping")
            fold_sharpes.append(0.0)
            _save_partial(ckpt, cfg["name"], fold_sharpes, best_sh)
            continue

        up = (tY==0).sum(); fl = (tY==1).sum(); dn = (tY==2).sum()
        print(f"  Fold {fold+1} | {len(tY)} samples | "
              f"UP={up/len(tY):.1%} FLAT={fl/len(tY):.1%} DOWN={dn/len(tY):.1%}")

        cnt     = np.bincount(tY, minlength=3)
        sw      = (1.0 / (cnt + 1e-8))[tY]
        sampler = WeightedRandomSampler(sw, num_samples=min(len(tY), 50000), replacement=True)
        tl = DataLoader(DS(tX, tXe, tY), batch_size=BATCH_SIZE, sampler=sampler)
        vl = DataLoader(DS(vX, vXe, vY), batch_size=BATCH_SIZE, shuffle=False)

        model = LSTMModel(hidden).to(device)
        cw    = torch.tensor([2.0, 1.0, 2.0]).to(device)
        crit  = nn.CrossEntropyLoss(weight=cw)
        opt   = torch.optim.Adam(model.parameters(), lr=LR, weight_decay=1e-5)
        sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)

        fold_best_sh = -999.0
        fold_best_st = None

        for ep in range(epochs):
            model.train()
            tl_ = 0
            for x, e, y in tl:
                x, e, y = x.to(device), e.to(device), y.to(device)
                opt.zero_grad()
                loss = crit(model(x, e), y)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
                tl_ += loss.item()
            sched.step()

            rets, acc, n_trades = sim_returns(model, vl, device)
            sh = compute_sharpe(rets)

            if sh > fold_best_sh:
                fold_best_sh = sh
                fold_best_st = {k: v.clone() for k, v in model.state_dict().items()}

            if (ep + 1) % 10 == 0:
                print(f"    Ep {ep+1:2d}/{epochs}  Loss:{tl_/len(tl):.4f}  "
                      f"Sharpe:{sh:.3f}  Trades:{n_trades}  Acc:{acc:.1%}")

            if sh > 3.0:
                print(f"    Overfit guard at Sharpe {sh:.2f} — stopping early")
                break

        fold_sharpes.append(fold_best_sh)
        print(f"  Fold {fold+1} best Sharpe: {fold_best_sh:.3f}")

        if fold_best_sh > best_sh:
            best_sh = fold_best_sh
            best_st = fold_best_st

        # ── CHECKPOINT after every fold ──────────────────────────────────
        # Safe to shut down here. Next run resumes from next fold.
        _save_partial(ckpt, cfg["name"], fold_sharpes, best_sh)
        print(f"  [checkpoint] Fold {fold+1} saved — safe to shut down now\n")

    return best_sh, best_st, fold_sharpes, thr


def _save_partial(ckpt, cfg_name, fold_sharpes, best_sh):
    """Save per-fold progress inside the checkpoint."""
    ckpt[f"partial_{cfg_name}"] = {
        "fold_sharpes": fold_sharpes,
        "best_sh":      best_sh,
        "saved_at":     datetime.now().isoformat()
    }
    save_checkpoint(ckpt)


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("GoldenTrinity LSTM Training v4 (with checkpointing)")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("Safe to Ctrl+C or close window — resumes from last fold")
    print("="*60)

    # Load data
    dfs = {}
    if os.path.exists(DATA_5MIN):
        dfs["5min"] = pd.read_csv(DATA_5MIN, index_col="timestamp", parse_dates=True)
        print(f"5-min data loaded: {len(dfs['5min'])} bars")
    if os.path.exists(DATA_1MIN):
        dfs["1min"] = pd.read_csv(DATA_1MIN, index_col="timestamp", parse_dates=True)
        print(f"1-min data loaded: {len(dfs['1min'])} bars")

    if not dfs:
        print("ERROR: No data files found in D:\\golden-trinity\\data\\")
        return

    diagnose_data(dfs)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")
    if torch.cuda.is_available():
        print(f"GPU: {torch.cuda.get_device_name(0)}")
    print()

    # Load checkpoint (resumes automatically if one exists)
    ckpt = load_checkpoint()

    configs_to_run = [c for c in CONFIGS if c["data"] in dfs]
    remaining = [c for c in configs_to_run
                 if c["name"] not in ckpt.get("completed_configs", [])]

    print(f"Total configs: {len(configs_to_run)} | "
          f"Already done: {len(configs_to_run)-len(remaining)} | "
          f"Remaining: {len(remaining)}\n")

    if not remaining:
        print("All configs already completed. Delete model/training_checkpoint.json to restart.")
        return

    if len(remaining) < len(configs_to_run):
        post_discord(DISCORD_HEALTH,
            f"▶️ **LSTM v4 training resumed** — "
            f"{len(remaining)}/{len(configs_to_run)} configs remaining | "
            f"{datetime.now().strftime('%H:%M')}")
    else:
        post_discord(DISCORD_HEALTH,
            f"🧠 **LSTM v4 training started** | {len(configs_to_run)} configs | "
            f"Device: {device} | {datetime.now().strftime('%H:%M')}")

    # Restore global best from checkpoint
    global_best_sh  = ckpt.get("global_best_sh",  -999.0)
    global_best_cfg = ckpt.get("global_best_cfg", None)
    global_best_hid = ckpt.get("global_best_hid", 64)

    for i, cfg in enumerate(remaining):
        print(f"\n{'='*60}")
        print(f"Config {i+1}/{len(remaining)}: {cfg['name']}")
        print(f"  data={cfg['data']} window={cfg['window']} forward={cfg['forward']} "
              f"hidden={cfg['hidden']} p{cfg['percentile']} epochs={cfg['epochs']}")
        print(f"{'='*60}")

        t0 = time.time()
        try:
            best_sh, best_st, fold_sharpes, thr = run_config(cfg, dfs, device, ckpt)
            elapsed = round((time.time() - t0) / 60, 1)

            result = {
                "config":         cfg["name"],
                "best_sharpe":    round(best_sh, 3),
                "fold_sharpes":   [round(s, 3) for s in fold_sharpes],
                "threshold_used": round(thr, 5),
                "elapsed_min":    elapsed,
            }
            ckpt["all_results"].append(result)

            # Mark config as fully complete
            ckpt["completed_configs"].append(cfg["name"])

            if best_sh > global_best_sh and best_st is not None:
                global_best_sh  = best_sh
                global_best_cfg = cfg["name"]
                global_best_hid = cfg["hidden"]
                ckpt["global_best_sh"]  = global_best_sh
                ckpt["global_best_cfg"] = global_best_cfg
                ckpt["global_best_hid"] = global_best_hid

                m = LSTMModel(cfg["hidden"])
                m.load_state_dict(best_st)
                torch.save(m.state_dict(), MODEL_FILE)
                print(f"  *** NEW BEST saved to lstm_model.pt ***")

            save_checkpoint(ckpt)
            print(f"\n  {cfg['name']}: Sharpe={best_sh:.3f} ({elapsed}min)")

            emoji = "✅" if best_sh >= 1.5 else ("⚠️" if best_sh > 0 else "❌")
            post_discord(DISCORD_HEALTH,
                f"{emoji} **{cfg['name']}** Sharpe={best_sh:.3f} ({elapsed}min)")

        except KeyboardInterrupt:
            print(f"\n[checkpoint] Interrupted — progress saved. Run again to resume.")
            save_checkpoint(ckpt)
            return
        except Exception as ex:
            import traceback
            print(f"  ERROR: {ex}")
            traceback.print_exc()
            ckpt["all_results"].append({"config": cfg["name"], "error": str(ex)})
            save_checkpoint(ckpt)

    # Save final metrics
    json.dump({
        "completed":   datetime.now().isoformat(),
        "best_config": global_best_cfg,
        "best_sharpe": round(global_best_sh, 3),
        "best_hidden": global_best_hid,
        "sim_gate":    {"high": SIM_HIGH, "low": SIM_LOW},
        "all_results": ckpt["all_results"],
    }, open(METRICS_FILE, "w"), indent=2)

    lines = []
    for r in ckpt["all_results"]:
        if "error" in r:
            lines.append(f"❌ {r['config']}: {r['error'][:60]}")
        else:
            e = "✅" if r["best_sharpe"] >= 1.5 else ("⚠️" if r["best_sharpe"] > 0 else "❌")
            lines.append(f"{e} {r['config']}: Sharpe={r['best_sharpe']}")

    post_discord(DISCORD_REPORTS,
        f"🏁 **LSTM v4 Complete**\n"
        f"Best: **{global_best_cfg}** Sharpe={global_best_sh:.3f}\n"
        f"Target: 1.5–2.0 | Overfit: >3.0\n\n" + "\n".join(lines))

    print(f"\n{'='*60}")
    print(f"DONE — Best: {global_best_cfg} Sharpe={global_best_sh:.3f}")
    if global_best_sh >= 1.5:
        print("✅ In target range — restart signal_service to load LSTM")
    elif global_best_sh > 0:
        print("⚠️  Positive — progress, but below target")
    else:
        print("❌ Still negative — paste diagnosis output to Claude")
    print(f"Results: {METRICS_FILE}")

    clear_checkpoint()


if __name__ == "__main__":
    main()
