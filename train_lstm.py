"""
train_lstm.py v3 - GoldenTrinity LSTM Training
Key fix: dual confidence gate in Sharpe sim (same as live system)
Only fires signal when p_up >= gate AND p_down <= low_gate (or vice versa)
Run: python train_lstm.py
"""
import os, json
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, WeightedRandomSampler
from datetime import datetime

DATA_FILE    = r"D:\golden-trinity\data\GLD_1min.csv"
MODEL_DIR    = r"D:\golden-trinity\model"
MODEL_FILE   = os.path.join(MODEL_DIR, "lstm_model.pt")
METRICS_FILE = os.path.join(MODEL_DIR, "training_metrics.json")

TICK_WINDOW   = 60
HIDDEN_SIZE   = 64
NUM_LAYERS    = 2
DROPOUT       = 0.2
BATCH_SIZE    = 512
EPOCHS        = 50
LR            = 0.001
FORWARD_BARS  = 15
THRESHOLD_PCT = 0.0015

# Dual gate for Sharpe simulation (mirrors live system, slightly looser for training)
TRAIN_HIGH    = 0.50   # p_up or p_down must exceed this
TRAIN_LOW     = 0.25   # opposing probability must be below this

os.makedirs(MODEL_DIR, exist_ok=True)


class LSTMModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.lstm = nn.LSTM(input_size=4, hidden_size=HIDDEN_SIZE,
                            num_layers=NUM_LAYERS, batch_first=True, dropout=DROPOUT)
        self.attention = nn.Linear(HIDDEN_SIZE, 1)
        self.fc = nn.Linear(HIDDEN_SIZE + 6, 3)
        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x, extra):
        out, _ = self.lstm(x)
        attn = torch.softmax(self.attention(out), dim=1)
        ctx = (attn * out).sum(dim=1)
        return self.softmax(self.fc(torch.cat([ctx, extra], dim=-1)))


class GoldDataset(Dataset):
    def __init__(self, X, extra, y):
        self.X = torch.tensor(X, dtype=torch.float32)
        self.extra = torch.tensor(extra, dtype=torch.float32)
        self.y = torch.tensor(y, dtype=torch.long)
    def __len__(self): return len(self.y)
    def __getitem__(self, i): return self.X[i], self.extra[i], self.y[i]


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


def make_label(closes, i):
    if i + FORWARD_BARS >= len(closes): return 1
    r = (closes[i + FORWARD_BARS] - closes[i]) / closes[i]
    return 0 if r > THRESHOLD_PCT else (2 if r < -THRESHOLD_PCT else 1)


def compute_sharpe(rets):
    r = np.array(rets)
    if len(r) < 5 or r.std() == 0: return 0.0
    return float(r.mean() / r.std() * np.sqrt(252 * 26))


def sim_returns(model, loader, device):
    """Dual gate: only fire when high confidence + low opposing probability."""
    model.eval()
    rets = []
    correct = total = 0
    with torch.no_grad():
        for x, e, y in loader:
            p = model(x.to(device), e.to(device))
            pu  = p[:, 0].cpu().numpy()
            pfl = p[:, 1].cpu().numpy()
            pd_ = p[:, 2].cpu().numpy()
            yt  = y.numpy()
            for i in range(len(pu)):
                # BUY signal: p_up high AND p_down low
                if pu[i] >= TRAIN_HIGH and pd_[i] <= TRAIN_LOW:
                    hit = yt[i] == 0
                    rets.append(0.0015 if hit else -0.0007)
                    correct += int(hit); total += 1
                # SELL signal: p_down high AND p_up low
                elif pd_[i] >= TRAIN_HIGH and pu[i] <= TRAIN_LOW:
                    hit = yt[i] == 2
                    rets.append(0.0015 if hit else -0.0007)
                    correct += int(hit); total += 1
    acc = correct / total if total > 0 else 0
    return rets, acc, total


def prepare(df, s, e):
    closes = df["close"].values
    vols   = df["volume"].values
    hours  = df.index.hour
    e9     = ema(closes, 9)
    e21    = ema(closes, 21)
    rm     = pd.Series(closes).rolling(20).mean().values
    rs     = pd.Series(closes).rolling(20).std().values
    X, Xe, Y = [], [], []
    for i in range(max(s, TICK_WINDOW + 21), e - FORWARD_BARS):
        wc  = closes[i - TICK_WINDOW:i]
        wv  = vols[i - TICK_WINDOW:i]
        ed  = (e9[i - TICK_WINDOW:i] - e21[i - TICK_WINDOW:i]) / (closes[i] + 1e-8)
        std = rs[i] if rs[i] > 0 else 1e-8
        bb  = (wc - rm[i]) / std
        np_ = (wc - wc.mean()) / (wc.std() + 1e-8)
        nv  = (wv - wv.mean()) / (wv.std() + 1e-8)
        Xe.append([0.0] + session_enc(int(hours[i])) + [1.0])
        X.append(np.stack([np_, nv, ed, bb], axis=-1))
        Y.append(make_label(closes, i))
    return np.array(X, dtype=np.float32), np.array(Xe, dtype=np.float32), np.array(Y)


def main():
    print("=" * 60)
    print("GoldenTrinity LSTM Training v3")
    print("=" * 60)

    if not os.path.exists(DATA_FILE):
        print("Run download_data.py first.")
        return

    df = pd.read_csv(DATA_FILE, index_col="timestamp", parse_dates=True)
    print(f"Loaded {len(df)} bars | {df.index[0].date()} to {df.index[-1].date()}")

    closes = df["close"].values
    al = [make_label(closes, i) for i in range(TICK_WINDOW, len(closes) - FORWARD_BARS)]
    t  = len(al)
    print(f"Labels: UP={al.count(0)/t:.1%} FLAT={al.count(1)/t:.1%} DOWN={al.count(2)/t:.1%}")
    print(f"Dual gate: p_high>={TRAIN_HIGH} AND p_low<={TRAIN_LOW}\n")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}\n")

    n  = len(df)
    fs = n // 4
    sharpes = []
    best_st = None
    best_sh = -999.0

    for fold in range(3):
        te = fs * (fold + 1)
        ve = min(te + fs, n)
        print(f"Fold {fold+1}: Train to {df.index[te-1].date()} | "
              f"Val {df.index[te].date()} to {df.index[ve-1].date()}")

        tX, tXe, tY = prepare(df, 0,  te)
        vX, vXe, vY = prepare(df, te, ve)

        if len(tY) < 1000:
            print("  Not enough data, skipping\n"); continue

        up, fl, dn = (tY==0).sum(), (tY==1).sum(), (tY==2).sum()
        print(f"  Train: UP={up} FLAT={fl} DOWN={dn}")

        cnt     = np.bincount(tY)
        sw      = (1.0 / (cnt + 1e-8))[tY]
        sampler = WeightedRandomSampler(sw, num_samples=min(len(tY), 60000), replacement=True)
        tl = DataLoader(GoldDataset(tX, tXe, tY), batch_size=BATCH_SIZE, sampler=sampler)
        vl = DataLoader(GoldDataset(vX, vXe, vY), batch_size=BATCH_SIZE, shuffle=False)

        model = LSTMModel().to(device)
        cw    = torch.tensor([2.5, 1.0, 2.5]).to(device)
        crit  = nn.CrossEntropyLoss(weight=cw)
        opt   = torch.optim.Adam(model.parameters(), lr=LR, weight_decay=1e-5)
        sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=EPOCHS)

        bsh = -999.0
        bst = None

        for ep in range(EPOCHS):
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
            if sh > bsh:
                bsh = sh
                bst = {k: v.clone() for k, v in model.state_dict().items()}

            if (ep + 1) % 10 == 0:
                print(f"  Ep {ep+1:2d}/{EPOCHS}  Loss:{tl_/len(tl):.4f}  "
                      f"Sharpe:{sh:.3f}  Trades:{n_trades}  Acc:{acc:.1%}")

            if sh > 3.0:
                print(f"  Overfit guard at Sharpe {sh:.2f}")
                break

        sharpes.append(bsh)
        print(f"  Fold {fold+1} best Sharpe: {bsh:.3f}\n")
        if bsh > best_sh:
            best_sh = bsh
            best_st = bst

    if best_st:
        m = LSTMModel()
        m.load_state_dict(best_st)
        torch.save(m.state_dict(), MODEL_FILE)
        print(f"Model saved to {MODEL_FILE}")

    avg = float(np.mean(sharpes)) if sharpes else 0.0
    json.dump({
        "trained_at":    datetime.now().isoformat(),
        "fold_sharpes":  [round(s, 3) for s in sharpes],
        "avg_sharpe":    round(avg, 3),
        "best_sharpe":   round(best_sh, 3),
        "forward_bars":  FORWARD_BARS,
        "threshold_pct": THRESHOLD_PCT,
        "bars":          len(df)
    }, open(METRICS_FILE, "w"), indent=2)

    print(f"\nFold Sharpes: {[round(s,3) for s in sharpes]}")
    print(f"Average: {avg:.3f}  Best: {best_sh:.3f}")
    if best_sh < 1.0:
        print("Below target — model learning, paste results for next steps")
    elif best_sh > 3.0:
        print("Above 3.0 — overfitting, reduce complexity")
    else:
        print("In target range 1.5-2.0 — restart signal_service to load LSTM")


if __name__ == "__main__":
    main()
