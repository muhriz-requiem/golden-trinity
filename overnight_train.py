"""
overnight_train.py — GoldenTrinity Overnight LSTM Search
Runs all night trying 6 hyperparameter configurations on 5-min GLD data.
Saves the best model. Sends Discord notification when done.
Run: python overnight_train.py

Key fix from first run (all Sharpe 0.0):
  - sim_returns gate was high=0.50, low=0.25 — too tight, 0 trades ever fired
  - Fixed to high=0.40, low=0.35 — model needs to fire trades to produce a Sharpe
  - THRESHOLD_PCT lowered to 0.001 — more samples get labeled UP/DOWN vs FLAT
"""
import os, json, time, requests
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, WeightedRandomSampler
from datetime import datetime
from itertools import product

DATA_FILE    = r"D:\golden-trinity\data\GLD_5min.csv"
MODEL_DIR    = r"D:\golden-trinity\model"
MODEL_FILE   = os.path.join(MODEL_DIR, "lstm_model.pt")
RESULTS_FILE = os.path.join(MODEL_DIR, "overnight_results.json")

DISCORD_HEALTH  = "<discord-webhook-health>"
DISCORD_REPORTS = "<discord-webhook-reports>"

os.makedirs(MODEL_DIR, exist_ok=True)

# 6 configurations to try overnight
# Thresholds are lower than v1 to generate more training labels
CONFIGS = [
    {"name": "A_baseline",    "window": 60,  "forward": 3,  "threshold": 0.001,  "hidden": 64,  "epochs": 60},
    {"name": "B_wider_label", "window": 60,  "forward": 6,  "threshold": 0.0015, "hidden": 64,  "epochs": 60},
    {"name": "C_deep",        "window": 60,  "forward": 3,  "threshold": 0.001,  "hidden": 128, "epochs": 60},
    {"name": "D_long_window", "window": 120, "forward": 6,  "threshold": 0.0015, "hidden": 64,  "epochs": 50},
    {"name": "E_tight_label", "window": 60,  "forward": 3,  "threshold": 0.0012, "hidden": 64,  "epochs": 60},
    {"name": "F_wide_deep",   "window": 120, "forward": 6,  "threshold": 0.0015, "hidden": 128, "epochs": 50},
]

# GATE THRESHOLDS FOR sim_returns
# v1: high=0.50, low=0.25 → 0 trades ever fired → Sharpe always 0.0
# v2: high=0.40, low=0.35 → model fires trades → Sharpe can be computed
SIM_HIGH = 0.40
SIM_LOW  = 0.35

def post_discord(webhook, msg):
    try:
        requests.post(webhook, json={"content": msg}, timeout=5)
    except:
        pass

def ema(arr, p):
    k = 2/(p+1); out = np.zeros(len(arr)); out[0] = arr[0]
    for i in range(1, len(arr)): out[i] = arr[i]*k + out[i-1]*(1-k)
    return out

def session_enc(h):
    if 0<=h<7:     return [1,0,0,0]
    elif 7<=h<13:  return [0,1,0,0]
    elif 13<=h<17: return [0,0,1,0]
    elif 17<=h<21: return [0,0,0,1]
    return [0,0,0,0]

def make_label(closes, i, fwd, thr):
    if i+fwd >= len(closes): return 1
    r = (closes[i+fwd]-closes[i])/closes[i]
    return 0 if r>thr else (2 if r<-thr else 1)

def sharpe(rets):
    r = np.array(rets)
    if len(r)<5 or r.std()==0: return 0.0
    # Annualise for 5-min bars (252 days * 78 bars/day)
    return float(r.mean()/r.std()*np.sqrt(252*78))

def sim_returns(model, loader, device, high=SIM_HIGH, low=SIM_LOW):
    """
    Simulate trading returns using the model.
    high/low thresholds control how confident model must be to fire a signal.
    Lower than live gate (0.60/0.20) to allow training signal to flow.
    """
    model.eval(); rets=[]; correct=total=0
    with torch.no_grad():
        for x,e,y in loader:
            p = model(x.to(device), e.to(device))
            pu=p[:,0].cpu().numpy(); pd_=p[:,2].cpu().numpy(); yt=y.numpy()
            for i in range(len(pu)):
                if pu[i]>=high and pd_[i]<=low:
                    h = yt[i]==0; rets.append(0.002 if h else -0.001); correct+=int(h); total+=1
                elif pd_[i]>=high and pu[i]<=low:
                    h = yt[i]==2; rets.append(0.002 if h else -0.001); correct+=int(h); total+=1
    acc = correct/total if total>0 else 0
    return rets, acc, total

def prepare(df, s, e, window, fwd, thr):
    closes=df["close"].values; vols=df["volume"].values; hours=df.index.hour
    e9=ema(closes,9); e21=ema(closes,21)
    rm=pd.Series(closes).rolling(20).mean().values
    rs=pd.Series(closes).rolling(20).std().values
    X,Xe,Y=[],[],[]
    for i in range(max(s,window+21), e-fwd):
        wc=closes[i-window:i]; wv=vols[i-window:i]
        ed=(e9[i-window:i]-e21[i-window:i])/(closes[i]+1e-8)
        std=rs[i] if rs[i]>0 else 1e-8; bb=(wc-rm[i])/std
        np_=(wc-wc.mean())/(wc.std()+1e-8); nv=(wv-wv.mean())/(wv.std()+1e-8)
        Xe.append([0.0]+session_enc(int(hours[i]))+[1.0])
        X.append(np.stack([np_,nv,ed,bb],axis=-1))
        Y.append(make_label(closes,i,fwd,thr))
    return np.array(X,dtype=np.float32), np.array(Xe,dtype=np.float32), np.array(Y)

class LSTMModel(nn.Module):
    def __init__(self, hidden):
        super().__init__()
        self.lstm = nn.LSTM(input_size=4, hidden_size=hidden, num_layers=2,
                            batch_first=True, dropout=0.2)
        self.attn = nn.Linear(hidden, 1)
        self.fc   = nn.Linear(hidden+6, 3)
        self.soft = nn.Softmax(dim=-1)
    def forward(self, x, extra):
        out,_ = self.lstm(x)
        a = torch.softmax(self.attn(out), dim=1)
        ctx = (a*out).sum(dim=1)
        return self.soft(self.fc(torch.cat([ctx,extra],dim=-1)))

class DS(Dataset):
    def __init__(self,X,e,y):
        self.X=torch.tensor(X,dtype=torch.float32)
        self.e=torch.tensor(e,dtype=torch.float32)
        self.y=torch.tensor(y,dtype=torch.long)
    def __len__(self): return len(self.y)
    def __getitem__(self,i): return self.X[i],self.e[i],self.y[i]

def run_config(cfg, df, device):
    window=cfg["window"]; fwd=cfg["forward"]; thr=cfg["threshold"]
    hidden=cfg["hidden"]; epochs=cfg["epochs"]
    n=len(df); fs=n//4
    fold_sharpes=[]; best_st=None; best_sh=-999.0

    for fold in range(3):
        te=fs*(fold+1); ve=min(te+fs,n)
        tX,tXe,tY = prepare(df,0,te,window,fwd,thr)
        vX,vXe,vY = prepare(df,te,ve,window,fwd,thr)
        if len(tY)<500: continue

        up,fl,dn = (tY==0).sum(), (tY==1).sum(), (tY==2).sum()
        print(f"  Fold{fold+1} labels: UP={up} FLAT={fl} DOWN={dn} (total={len(tY)})")

        cnt=np.bincount(tY); sw=(1.0/(cnt+1e-8))[tY]
        sampler=WeightedRandomSampler(sw,num_samples=min(len(tY),40000),replacement=True)
        tl=DataLoader(DS(tX,tXe,tY),batch_size=512,sampler=sampler)
        vl=DataLoader(DS(vX,vXe,vY),batch_size=512,shuffle=False)

        model=LSTMModel(hidden).to(device)
        cw=torch.tensor([2.5,1.0,2.5]).to(device)
        crit=nn.CrossEntropyLoss(weight=cw)
        opt=torch.optim.Adam(model.parameters(),lr=0.001,weight_decay=1e-5)
        sched=torch.optim.lr_scheduler.CosineAnnealingLR(opt,T_max=epochs)

        bsh=-999.0; bst=None
        for ep in range(epochs):
            model.train(); tl_=0
            for x,e,y in tl:
                x,e,y=x.to(device),e.to(device),y.to(device)
                opt.zero_grad(); loss=crit(model(x,e),y)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(),1.0)
                opt.step(); tl_+=loss.item()
            sched.step()
            rets,acc,n_tr=sim_returns(model,vl,device)
            sh=sharpe(rets)
            if sh>bsh: bsh=sh; bst={k:v.clone() for k,v in model.state_dict().items()}
            if (ep+1)%10==0:
                print(f"  [{cfg['name']}] Fold{fold+1} Ep{ep+1}/{epochs} "
                      f"Loss:{tl_/len(tl):.4f} Sharpe:{sh:.3f} Trades:{n_tr} Acc:{acc:.1%}")
            if sh>3.0: print(f"  Overfit guard at Sharpe {sh:.2f}"); break

        fold_sharpes.append(bsh)
        if bsh>best_sh: best_sh=bsh; best_st=bst

    return best_sh, best_st, fold_sharpes

def main():
    print("="*60)
    print("GoldenTrinity Overnight Training v2")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Gate thresholds: high={SIM_HIGH} low={SIM_LOW} (v1 was 0.50/0.25 — caused 0 trades)")
    print(f"Configs to run: {len(CONFIGS)}")
    print("="*60)

    if not os.path.exists(DATA_FILE):
        print(f"ERROR: {DATA_FILE} not found. Run download_5min.py first.")
        return

    df = pd.read_csv(DATA_FILE, index_col="timestamp", parse_dates=True)
    print(f"Loaded {len(df)} 5-min bars | {df.index[0].date()} to {df.index[-1].date()}\n")

    # Show label distribution at default threshold to confirm labels fire
    closes = df["close"].values
    test_labels = [make_label(closes, i, 3, 0.001) for i in range(60, min(len(closes)-3, 1000))]
    up = test_labels.count(0); fl = test_labels.count(1); dn = test_labels.count(2)
    print(f"Sample label distribution (thr=0.001, fwd=3): UP={up/len(test_labels):.1%} FLAT={fl/len(test_labels):.1%} DOWN={dn/len(test_labels):.1%}")
    print(f"(If FLAT > 80%, labels are too tight — raise threshold)\n")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}\n")

    post_discord(DISCORD_HEALTH,
        f"🌙 **GoldenTrinity overnight training v2 started**\n"
        f"{len(CONFIGS)} configs | {len(df)} 5-min bars | Device: {device}\n"
        f"Gate fix: high={SIM_HIGH} low={SIM_LOW}\n"
        f"Started: {datetime.now().strftime('%H:%M')}")

    all_results = []
    global_best_sh = -999.0
    global_best_st = None
    global_best_cfg = None
    global_best_hidden = 64

    for i, cfg in enumerate(CONFIGS):
        print(f"\n{'='*60}")
        print(f"Config {i+1}/{len(CONFIGS)}: {cfg['name']}")
        print(f"  window={cfg['window']} forward={cfg['forward']} "
              f"threshold={cfg['threshold']} hidden={cfg['hidden']} epochs={cfg['epochs']}")
        print(f"  Time: {datetime.now().strftime('%H:%M')}")
        print(f"{'='*60}")

        t0 = time.time()
        try:
            best_sh, best_st, fold_sharpes = run_config(cfg, df, device)
            elapsed = round((time.time()-t0)/60, 1)
            result = {
                "config": cfg["name"],
                "best_sharpe": round(best_sh, 3),
                "fold_sharpes": [round(s,3) for s in fold_sharpes],
                "elapsed_min": elapsed,
                "threshold": cfg["threshold"],
                "hidden": cfg["hidden"]
            }
            all_results.append(result)
            print(f"\n  {cfg['name']}: best Sharpe={best_sh:.3f} ({elapsed} min)")

            if best_sh > global_best_sh and best_st is not None:
                global_best_sh = best_sh
                global_best_st = best_st
                global_best_cfg = cfg["name"]
                global_best_hidden = cfg["hidden"]

                m = LSTMModel(cfg["hidden"])
                m.load_state_dict(best_st)
                torch.save(m.state_dict(), MODEL_FILE)
                print(f"  *** NEW BEST — saved to {MODEL_FILE} ***")

            status = "✅" if best_sh >= 1.5 else ("⚠️" if best_sh > 0 else "❌")
            post_discord(DISCORD_HEALTH,
                f"{status} **Config {cfg['name']} done** — Sharpe: {best_sh:.3f} ({elapsed}min)")

        except Exception as ex:
            print(f"  ERROR in {cfg['name']}: {ex}")
            all_results.append({"config": cfg["name"], "error": str(ex)})

    json.dump({
        "completed": datetime.now().isoformat(),
        "best_config": global_best_cfg,
        "best_sharpe": round(global_best_sh, 3),
        "best_hidden": global_best_hidden,
        "sim_gate_high": SIM_HIGH,
        "sim_gate_low": SIM_LOW,
        "all_results": all_results,
        "model_file": MODEL_FILE
    }, open(RESULTS_FILE, "w"), indent=2)

    lines = []
    for r in all_results:
        if "error" in r:
            lines.append(f"❌ {r['config']}: ERROR — {r['error'][:50]}")
        else:
            emoji = "✅" if r["best_sharpe"] >= 1.5 else ("⚠️" if r["best_sharpe"] > 0 else "❌")
            lines.append(f"{emoji} {r['config']}: Sharpe={r['best_sharpe']}")

    summary = (
        f"🌅 **GoldenTrinity Training v2 Complete**\n"
        f"Best config: **{global_best_cfg}** — Sharpe: **{global_best_sh:.3f}**\n"
        f"Target range: 1.5–2.0 (above 3.0 = overfitting)\n\n"
        + "\n".join(lines)
        + f"\n\nCompleted: {datetime.now().strftime('%H:%M')}"
    )
    post_discord(DISCORD_REPORTS, summary)

    print(f"\n{'='*60}")
    print(f"OVERNIGHT TRAINING COMPLETE")
    print(f"Best: {global_best_cfg} — Sharpe {global_best_sh:.3f}")
    print(f"Results: {RESULTS_FILE}")
    print(f"Model:   {MODEL_FILE}")
    if global_best_sh >= 1.5:
        print("✅ In target range — restart signal_service to load LSTM")
    elif global_best_sh > 0:
        print("⚠️  Positive but below target — rule-based still better, keep paper trading")
    else:
        print("❌ Still negative — stick with rule-based signals, try Google Colab GPU")


if __name__ == "__main__":
    main()
