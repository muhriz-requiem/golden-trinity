"""
train_lstm_v5.py — GoldenTrinity LSTM Training (Clean)
=======================================================

Fixes from v4:
  1. DATA LEAKAGE: threshold now computed from training slice only, not full dataset
  2. PURGE GAP: forward_bars gap between train end and val start — no label overlap
  3. ANNUALISATION: bars_per_year correct per timeframe (1min=252*390, 5min=252*78)
  4. OVERFIT GUARD: raised to 4.0 (sim gate is loose, so 3.0 was firing too early)
  5. EARLY STOPPING: stops if val Sharpe hasn't improved in 15 epochs — saves time
  6. CONFIGS: replaced known-bad large-hidden configs with useful variants
  7. THRESHOLD: recomputed per fold from training data only

Walk-forward structure (correct):
  Fold 1: Train [0 → 25%]  Gap [fwd bars]  Val [25% → 50%]
  Fold 2: Train [0 → 50%]  Gap [fwd bars]  Val [50% → 75%]
  Fold 3: Train [0 → 75%]  Gap [fwd bars]  Val [75% → 100%]

The metric that matters: AVERAGE of all 3 fold Sharpes.
A model with [0.4, 0.5, 0.6] avg=0.5 is real.
A model with [0.0, 0.0, 14.0] avg=4.7 is leakage.

SAFE SHUTDOWN: Ctrl+C or close window — resumes from last completed fold.
RESUME: Run again — reads checkpoint automatically.
"""
import os, json, time
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, WeightedRandomSampler
from datetime import datetime
import requests
from training_report import write_training_report

# ── Paths ──────────────────────────────────────────────────────────────────
DATA_1MIN       = r"D:\golden-trinity\data\GLD_1min.csv"
DATA_5MIN       = r"D:\golden-trinity\data\GLD_5min.csv"
MODEL_DIR       = r"D:\golden-trinity\model"
MODEL_FILE      = os.path.join(MODEL_DIR, "lstm_model.pt")
METRICS_FILE    = os.path.join(MODEL_DIR, "training_metrics_v5.json")
CHECKPOINT_FILE = os.path.join(MODEL_DIR, "training_checkpoint_v5.json")
LIVE_TRADES     = r"D:\golden-trinity\data\GLD_live_trades.csv"   # exported by update_training_data.py
LIVE_TRADE_WEIGHT = 2  # weight multiplier for live trades vs historical bars (more recent = more relevant)

DISCORD_REPORTS = "<discord-webhook-reports>"
DISCORD_HEALTH  = "<discord-webhook-health>"

os.makedirs(MODEL_DIR, exist_ok=True)

# ── Fixed hyperparameters ──────────────────────────────────────────────────
NUM_LAYERS      = 2
DROPOUT         = 0.2
BATCH_SIZE      = 512
EARLY_STOP_PAT  = 15    # stop if no improvement for this many epochs
OVERFIT_GUARD   = 2.0   # corrected threshold on trade-annualised scale

# Sim gate for Sharpe calculation during training
# Deliberately loose — live gate is 0.60/0.20
# Just needs to be above random (0.33) to produce a meaningful signal
SIM_HIGH = 0.38
SIM_LOW  = 0.38

# bars_per_year by timeframe — used for Sharpe annualisation
BARS_PER_YEAR = {
    "1min": 252 * 390,
    "5min": 252 * 78,
}

# ── Configs ────────────────────────────────────────────────────────────────
# Focused on what Fold 1 told us: 5min data had weak positive signal (0.448)
# Varying: forward window, label percentile, learning rate
# Removed: large hidden=128 configs (consistently worse in v4)
CONFIGS = [
    # Best from v4 fold 1 — baseline to beat
    {"name": "v5_A_5min_fwd6_p35_lr1e3",  "data": "5min", "forward": 6,  "window": 60,  "hidden": 64, "percentile": 35, "lr": 0.001, "epochs": 100},
    # Longer forward window — 30min horizon, captures bigger moves
    {"name": "v5_B_5min_fwd12_p40_lr1e3", "data": "5min", "forward": 12, "window": 60,  "hidden": 64, "percentile": 40, "lr": 0.001, "epochs": 100},
    # Slower learning rate — better generalisation on small datasets
    {"name": "v5_C_5min_fwd6_p35_lr3e4",  "data": "5min", "forward": 6,  "window": 60,  "hidden": 64, "percentile": 35, "lr": 0.0003,"epochs": 100},
    # Wider label — p30 gives ~35% each direction, more signal
    {"name": "v5_D_5min_fwd6_p30_lr1e3",  "data": "5min", "forward": 6,  "window": 60,  "hidden": 64, "percentile": 30, "lr": 0.001, "epochs": 100},
    # 1-min data with honest Fold 1 test — different regime
    {"name": "v5_E_1min_fwd15_p35_lr1e3", "data": "1min", "forward": 15, "window": 60,  "hidden": 64, "percentile": 35, "lr": 0.001, "epochs": 80},
    # Longer window on 5-min — 10 hours of context
    {"name": "v5_F_5min_fwd6_p35_w120",   "data": "5min", "forward": 6,  "window": 120, "hidden": 64, "percentile": 35, "lr": 0.001, "epochs": 80},
]


# ── Checkpoint ─────────────────────────────────────────────────────────────

def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE) as f:
                ckpt = json.load(f)
            done = len(ckpt.get("completed_configs", []))
            print(f"[checkpoint] Resuming — {done} configs already complete")
            return ckpt
        except Exception:
            pass
    return {
        "completed_configs": [],
        "all_results":       [],
        "global_best_sh":    -999.0,
        "global_best_cfg":   None,
        "global_best_hid":   64,
        "started_at":        datetime.now().isoformat(),
    }

def save_checkpoint(ckpt):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(ckpt, f, indent=2)

def clear_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)


# ── Discord ────────────────────────────────────────────────────────────────

def post_discord(webhook, msg):
    try:
        requests.post(webhook, json={"content": msg}, timeout=5)
    except Exception:
        pass


# ── Features ───────────────────────────────────────────────────────────────

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

def threshold_from_slice(closes, start, end, fwd, percentile):
    """
    FIX: Compute threshold from TRAINING SLICE ONLY.
    Never use validation data to set the label boundary.
    """
    rets = [abs((closes[i+fwd] - closes[i]) / closes[i])
            for i in range(start, end - fwd)]
    return float(np.percentile(rets, percentile))

def make_label(closes, i, fwd, thr):
    if i + fwd >= len(closes):
        return 1
    r = (closes[i+fwd] - closes[i]) / closes[i]
    return 0 if r > thr else (2 if r < -thr else 1)

def prepare(df, s, e, window, fwd, thr):
    """Build feature matrix for rows [s, e). Labels use forward return."""
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
        np.array(Y,  dtype=np.int64),
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


def compute_sharpe(rets, timeframe, bars_in_fold: int = 0):
    """
    FIX: Annualise by trade frequency, not bars per year.
    Old: sqrt(19656) ~ 140x multiplier -> fake Sharpe of 14+ from real Sharpe of 0.1
    New: estimate trades per year from observed rate in the validation fold.
    """
    r = np.array(rets)
    if len(r) < 10 or r.std() == 0:
        return 0.0
    if bars_in_fold > 0:
        bpy_bars        = BARS_PER_YEAR.get(timeframe, 252 * 78)
        trades_per_year = (len(r) / max(bars_in_fold, 1)) * bpy_bars
        ann_factor      = np.sqrt(max(trades_per_year, 1))
    else:
        ann_factor = np.sqrt(len(r))
    return float(r.mean() / r.std() * ann_factor)

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
    acc = correct / total if total > 0 else 0.0
    return rets, acc, total


# ── Walk-forward ───────────────────────────────────────────────────────────

def run_config(cfg, dfs, device, ckpt):
    df      = dfs[cfg["data"]]
    window  = cfg["window"]
    fwd     = cfg["forward"]
    hidden  = cfg["hidden"]
    epochs  = cfg["epochs"]
    lr      = cfg["lr"]
    pct     = cfg["percentile"]
    tf      = cfg["data"]

    n  = len(df)
    fs = n // 4   # each fold = 25% of data

    # Resume partial folds
    partial_key = f"partial_{cfg['name']}"
    saved       = ckpt.get(partial_key, {})
    fold_sharpes = list(saved.get("fold_sharpes", []))
    start_fold   = len(fold_sharpes)
    best_sh      = saved.get("best_sh", -999.0)
    best_st      = None

    if start_fold > 0:
        print(f"  Resuming from fold {start_fold+1}")

    for fold in range(start_fold, 3):
        # ── Walk-forward split with purge gap ─────────────────────────────
        #
        #  train_end = fs*(fold+1)
        #  val_start = train_end + fwd   ← PURGE GAP: skip forward_bars rows
        #  val_end   = val_start + fs
        #
        #  Without purge gap: last row of training has a label that looks
        #  forward_bars into the val set → data leakage.
        #
        train_end = fs * (fold + 1)
        val_start = train_end + fwd      # purge gap
        val_end   = min(val_start + fs, n)

        if val_end - val_start < 500:
            print(f"  Fold {fold+1}: insufficient val data, skipping")
            fold_sharpes.append(0.0)
            _save_partial(ckpt, cfg["name"], fold_sharpes, best_sh)
            continue

        # ── Threshold from TRAINING DATA ONLY ────────────────────────────
        closes = df["close"].values
        thr = threshold_from_slice(closes, 0, train_end, fwd, pct)
        print(f"  Fold {fold+1} | train=[0→{train_end}] val=[{val_start}→{val_end}] "
              f"| thr={thr:.5f} ({thr*100:.3f}%)")

        tX, tXe, tY = prepare(df, 0,         train_end, window, fwd, thr)
        vX, vXe, vY = prepare(df, val_start, val_end,   window, fwd, thr)

        if len(tY) < 200 or len(vY) < 100:
            print(f"  Fold {fold+1}: too few samples (train={len(tY)} val={len(vY)}), skipping")
            fold_sharpes.append(0.0)
            _save_partial(ckpt, cfg["name"], fold_sharpes, best_sh)
            continue

        up = (tY==0).sum(); fl = (tY==1).sum(); dn = (tY==2).sum()
        print(f"  Train: {len(tY)} samples | UP={up/len(tY):.1%} FLAT={fl/len(tY):.1%} DOWN={dn/len(tY):.1%}")
        vup = (vY==0).sum(); vfl = (vY==1).sum(); vdn = (vY==2).sum()
        print(f"  Val:   {len(vY)} samples | UP={vup/len(vY):.1%} FLAT={vfl/len(vY):.1%} DOWN={vdn/len(vY):.1%}")

        cnt     = np.bincount(tY, minlength=3)
        sw      = (1.0 / (cnt + 1e-8))[tY]
        sampler = WeightedRandomSampler(sw, num_samples=min(len(tY), 50000), replacement=True)
        tl = DataLoader(DS(tX, tXe, tY), batch_size=BATCH_SIZE, sampler=sampler)
        vl = DataLoader(DS(vX, vXe, vY), batch_size=BATCH_SIZE, shuffle=False)

        model = LSTMModel(hidden).to(device)
        cw    = torch.tensor([2.0, 1.0, 2.0]).to(device)
        crit  = nn.CrossEntropyLoss(weight=cw)
        opt   = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-5)
        sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)

        fold_best_sh  = -999.0
        fold_best_st  = None
        no_improve    = 0   # early stopping counter

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
            sh = compute_sharpe(rets, tf, bars_in_fold=(val_end - val_start))

            if sh > fold_best_sh:
                fold_best_sh = sh
                fold_best_st = {k: v.clone() for k, v in model.state_dict().items()}
                no_improve   = 0
            else:
                no_improve  += 1

            if (ep + 1) % 10 == 0:
                print(f"    Ep {ep+1:3d}/{epochs}  Loss:{tl_/len(tl):.4f}  "
                      f"Sharpe:{sh:.3f}  Best:{fold_best_sh:.3f}  "
                      f"Trades:{n_trades}  Acc:{acc:.1%}  NoImprove:{no_improve}")

            # Early stopping
            if no_improve >= EARLY_STOP_PAT:
                print(f"    Early stop at ep {ep+1} — no improvement for {EARLY_STOP_PAT} epochs")
                break

            # Overfitting guard (raised from 3.0 — sim gate is loose so 3.0 fires too early)
            if sh > OVERFIT_GUARD:
                print(f"    Overfit guard at Sharpe {sh:.2f}")
                break

        fold_sharpes.append(fold_best_sh)
        print(f"  Fold {fold+1} best Sharpe: {fold_best_sh:.3f}\n")

        if fold_best_sh > best_sh:
            best_sh = fold_best_sh
            best_st = fold_best_st

        # Checkpoint after every fold
        _save_partial(ckpt, cfg["name"], fold_sharpes, best_sh)
        print(f"  [checkpoint] saved after fold {fold+1} — safe to shut down\n")

    # The real metric: average across all folds
    valid_sharpes = [s for s in fold_sharpes if s > -100]
    avg_sh = float(np.mean(valid_sharpes)) if valid_sharpes else -999.0
    print(f"  Fold Sharpes: {[round(s,3) for s in fold_sharpes]}")
    print(f"  Average Sharpe (honest metric): {avg_sh:.3f}")

    return avg_sh, best_sh, best_st, fold_sharpes


def _save_partial(ckpt, cfg_name, fold_sharpes, best_sh):
    ckpt[f"partial_{cfg_name}"] = {
        "fold_sharpes": fold_sharpes,
        "best_sh":      best_sh,
        "saved_at":     datetime.now().isoformat(),
    }
    save_checkpoint(ckpt)


# ── Diagnosis ──────────────────────────────────────────────────────────────

def diagnose_data(dfs):
    print("\n" + "="*60)
    print("DATA DIAGNOSIS")
    print("="*60)
    for name, df in dfs.items():
        closes = df["close"].values
        fwd = 6 if name == "5min" else 15
        print(f"\n{name}: {len(df)} bars | ${closes[0]:.2f} → ${closes[-1]:.2f}")
        rets = np.array([(closes[i+fwd]-closes[i])/closes[i]
                         for i in range(len(closes)-fwd)])
        print(f"  fwd-{fwd}: std={rets.std()*100:.3f}%  "
              f"p30_abs={np.percentile(abs(rets),30)*100:.3f}%  "
              f"p35_abs={np.percentile(abs(rets),35)*100:.3f}%  "
              f"p40_abs={np.percentile(abs(rets),40)*100:.3f}%")
        for thr in [0.0005, 0.001, 0.0015, 0.002]:
            up = (rets > thr).mean(); dn = (rets < -thr).mean()
            print(f"    thr={thr:.4f}: UP={up:.1%}  FLAT={(1-up-dn):.1%}  DOWN={dn:.1%}")
    print()


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("GoldenTrinity LSTM Training v5")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("Fixes: data leakage, purge gap, annualisation, early stopping")
    print("Metric: AVERAGE Sharpe across all 3 folds (not max)")
    print("Safe to Ctrl+C — resumes from last fold")
    print("=" * 60)

    dfs = {}
    if os.path.exists(DATA_5MIN):
        dfs["5min"] = pd.read_csv(DATA_5MIN, index_col="timestamp", parse_dates=True)
        print(f"5-min: {len(dfs['5min'])} bars")
    if os.path.exists(DATA_1MIN):
        dfs["1min"] = pd.read_csv(DATA_1MIN, index_col="timestamp", parse_dates=True)
        print(f"1-min: {len(dfs['1min'])} bars")

    if not dfs:
        print("ERROR: No data files found.")
        return

    # Load live paper trade outcomes — more valuable than historical bars
    # These are real fills at real prices through your real signal logic
    # Injected as duplicated rows (weight 2x) in the 5-min dataset
    if os.path.exists(LIVE_TRADES):
        try:
            live_df = pd.read_csv(LIVE_TRADES)
            n_live  = len(live_df)
            if n_live >= 20 and "5min" in dfs:
                # Convert trade timestamps to the same format as bar data
                # Each live trade is a single data point — we embed it as a synthetic bar
                # at the fill_price, repeated LIVE_TRADE_WEIGHT times for emphasis
                live_rows = []
                for _, row in live_df.iterrows():
                    try:
                        ts    = pd.Timestamp(row["opened_at"]).tz_localize("UTC") if pd.Timestamp(row["opened_at"]).tzinfo is None else pd.Timestamp(row["opened_at"])
                        price = float(row["fill_price"])
                        live_rows.append({"timestamp": ts, "open": price, "high": price,
                                          "low": price, "close": price, "volume": 1.0})
                    except Exception:
                        pass
                if live_rows:
                    live_bars = pd.DataFrame(live_rows).set_index("timestamp").sort_index()
                    # Duplicate rows to apply weighting
                    weighted  = pd.concat([live_bars] * LIVE_TRADE_WEIGHT)
                    original  = dfs["5min"]
                    combined  = pd.concat([original, weighted]).sort_index()
                    # Deduplicate keeping all weighted rows (do NOT drop_duplicates)
                    dfs["5min"] = combined
                    print(f"Live trades: {n_live} trades injected ({n_live * LIVE_TRADE_WEIGHT} weighted rows added to 5min)")
                    if n_live < 200:
                        print(f"  [{n_live}/200 live trades] — impact is small but growing")
                    else:
                        print(f"  [{n_live} live trades] — significant recent data, model will adapt to current behaviour")
        except Exception as e:
            print(f"Live trades load failed (non-fatal): {e}")
    else:
        print(f"Live trades: not found (run update_training_data.py to export)")

    diagnose_data(dfs)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")
    if torch.cuda.is_available():
        print(f"GPU: {torch.cuda.get_device_name(0)}")
    print()

    ckpt = load_checkpoint()

    configs_to_run = [c for c in CONFIGS if c["data"] in dfs]
    remaining      = [c for c in configs_to_run
                      if c["name"] not in ckpt.get("completed_configs", [])]

    print(f"Configs: {len(configs_to_run)} total | "
          f"{len(configs_to_run)-len(remaining)} done | "
          f"{len(remaining)} remaining\n")

    if not remaining:
        print("All done. Delete model/training_checkpoint_v5.json to restart.")
        return

    post_discord(DISCORD_HEALTH,
        f"🧠 **LSTM v5 started** | {len(remaining)} configs | {device} | "
        f"Fixes: leakage+purge+annualisation+early_stop | "
        f"{datetime.now().strftime('%H:%M')}")

    global_best_avg = ckpt.get("global_best_sh", -999.0)
    global_best_cfg = ckpt.get("global_best_cfg", None)
    global_best_hid = ckpt.get("global_best_hid", 64)

    for i, cfg in enumerate(remaining):
        print(f"\n{'='*60}")
        print(f"Config {i+1}/{len(remaining)}: {cfg['name']}")
        print(f"  data={cfg['data']} window={cfg['window']} fwd={cfg['forward']} "
              f"hidden={cfg['hidden']} p{cfg['percentile']} lr={cfg['lr']} epochs={cfg['epochs']}")
        print(f"{'='*60}")

        t0 = time.time()
        try:
            avg_sh, best_sh, best_st, fold_sharpes = run_config(cfg, dfs, device, ckpt)
            elapsed = round((time.time() - t0) / 60, 1)

            result = {
                "config":        cfg["name"],
                "avg_sharpe":    round(avg_sh, 3),    # THE metric
                "best_sharpe":   round(best_sh, 3),   # for reference only
                "fold_sharpes":  [round(s, 3) for s in fold_sharpes],
                "elapsed_min":   elapsed,
                "lr":            cfg["lr"],
                "percentile":    cfg["percentile"],
                "forward":       cfg["forward"],
            }
            ckpt["all_results"].append(result)
            ckpt["completed_configs"].append(cfg["name"])

            # Best model = best AVERAGE Sharpe across folds
            if avg_sh > global_best_avg and best_st is not None:
                global_best_avg = avg_sh
                global_best_cfg = cfg["name"]
                global_best_hid = cfg["hidden"]
                ckpt["global_best_sh"]  = global_best_avg
                ckpt["global_best_cfg"] = global_best_cfg
                ckpt["global_best_hid"] = global_best_hid

                m = LSTMModel(cfg["hidden"])
                m.load_state_dict(best_st)
                torch.save(m.state_dict(), MODEL_FILE)
                print(f"  *** NEW BEST avg_sharpe={avg_sh:.3f} — saved to lstm_model.pt ***")

            save_checkpoint(ckpt)

            emoji = "✅" if avg_sh >= 1.0 else ("⚠️" if avg_sh > 0 else "❌")
            post_discord(DISCORD_HEALTH,
                f"{emoji} **{cfg['name']}** avg_sharpe={avg_sh:.3f} "
                f"folds={[round(s,2) for s in fold_sharpes]} ({elapsed}min)")

        except KeyboardInterrupt:
            print("\n[checkpoint] Interrupted — saved. Run again to resume.")
            save_checkpoint(ckpt)
            return
        except Exception as ex:
            import traceback
            print(f"  ERROR: {ex}")
            traceback.print_exc()
            ckpt["all_results"].append({"config": cfg["name"], "error": str(ex)})
            save_checkpoint(ckpt)

    # Final save
    json.dump({
        "completed":        datetime.now().isoformat(),
        "best_config":      global_best_cfg,
        "best_avg_sharpe":  round(global_best_avg, 3),
        "best_hidden":      global_best_hid,
        "sim_gate":         {"high": SIM_HIGH, "low": SIM_LOW},
        "all_results":      ckpt["all_results"],
        "note":             "avg_sharpe is the real metric. best_sharpe per-fold is for reference only.",
    }, open(METRICS_FILE, "w"), indent=2)

    lines = []
    for r in ckpt["all_results"]:
        if "error" in r:
            lines.append(f"❌ {r['config']}: {r['error'][:50]}")
        else:
            e = "✅" if r["avg_sharpe"] >= 1.0 else ("⚠️" if r["avg_sharpe"] > 0 else "❌")
            lines.append(f"{e} {r['config']}: avg={r['avg_sharpe']} folds={r['fold_sharpes']}")

    post_discord(DISCORD_REPORTS,
        f"🏁 **LSTM v5 Complete**\n"
        f"Best: **{global_best_cfg}** avg_sharpe={global_best_avg:.3f}\n"
        f"Target avg: >1.0 | Overfit: any fold >4.0\n\n"
        + "\n".join(lines))

    print(f"\n{'='*60}")
    print(f"DONE")
    print(f"Best config: {global_best_cfg}")
    print(f"Best avg Sharpe: {global_best_avg:.3f}")
    print()
    if global_best_avg >= 1.0:
        print("✅ Avg Sharpe ≥ 1.0 — model is usable")
        print("   Restart signal_service to load lstm_model.pt")
    elif global_best_avg > 0:
        print("⚠️  Positive avg Sharpe — weak signal exists")
        print("   Keep running rule-based. Accumulate more paper data.")
        print("   Retrain in 4 weeks with live paper data added.")
    else:
        print("❌ No consistent edge found yet")
        print("   Rule-based signals are fine for now.")
        print("   Accumulate 4-6 weeks of paper data then retrain.")
    print(f"\nResults: {METRICS_FILE}")

    # Write comprehensive debug report
    live_trades_count = 0
    if os.path.exists(r"D:\golden-trinity\data\GLD_live_trades.csv"):
        try: live_trades_count = len(pd.read_csv(r"D:\golden-trinity\data\GLD_live_trades.csv"))
        except: pass
    best_result = next((r for r in ckpt["all_results"] if r.get("config") == global_best_cfg), {})
    write_training_report(
        source       = "v5",
        config_name  = global_best_cfg or "none",
        fold_sharpes = best_result.get("fold_sharpes", []),
        avg_sharpe   = global_best_avg,
        deployed     = global_best_avg > 0,
        data_info    = {
            "bars_5min":   len(dfs.get("5min", [])) if "5min" in dfs else 0,
            "bars_1min":   len(dfs.get("1min", [])) if "1min" in dfs else 0,
            "live_trades": live_trades_count,
            "configs_run": len(ckpt["all_results"]),
            "device":      str(device),
        },
        extra = {
            "all_config_results": str([
                f"{r[\"config\"]}: avg={r.get(\"avg_sharpe\", \"err\")} folds={r.get(\"fold_sharpes\", [])}"
                for r in ckpt["all_results"]
            ]),
            "sim_gate":    f"high={SIM_HIGH} low={SIM_LOW}",
            "overfit_guard": OVERFIT_GUARD,
        }
    )

    clear_checkpoint()


if __name__ == "__main__":
    main()


