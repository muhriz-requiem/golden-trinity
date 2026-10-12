"""
training_report.py — GoldenTrinity Training Report Writer
==========================================================
Called after every training run (both continuous xauusd_service training
and manual train_lstm_v5.py runs) to write a comprehensive debug report.

Report file: D:\golden-trinity\model\training_report.txt
Each run APPENDS to the file so you have a full history.
The file can be read and pasted to Claude for debugging.

Usage:
    from training_report import write_training_report
    write_training_report(
        source      = "continuous",        # "continuous" or "v5"
        config_name = "v5_A_5min_fwd6",
        fold_sharpes = [0.42, 0.51, 0.38],
        avg_sharpe   = 0.437,
        deployed     = True,
        data_info    = {"bars_5min": 26000, "live_trades": 23},
        extra        = {"epochs_run": [47, 60, 31], "trade_counts": [120, 145, 98]},
        error        = None
    )
"""
import os
import json
from datetime import datetime, timezone

REPORT_FILE = r"D:\golden-trinity\model\training_report.txt"
MAX_REPORT_BYTES = 500_000   # ~500KB — trim oldest entries if file grows too large


def write_training_report(
    source: str,          # "continuous" or "v5"
    config_name: str,
    fold_sharpes: list,
    avg_sharpe: float,
    deployed: bool,
    data_info: dict,
    extra: dict = None,
    error: str = None,
):
    """
    Append a comprehensive training report entry to training_report.txt.

    Parameters
    ----------
    source       : "continuous" (xauusd_service auto-retrain) or "v5" (manual run)
    config_name  : name of the config that produced this result
    fold_sharpes : list of per-fold Sharpe values, e.g. [0.42, 0.51, 0.38]
    avg_sharpe   : mean of fold_sharpes (the real metric)
    deployed     : True if this model was hot-swapped into production
    data_info    : dict with data stats — bars used, live trades, etc.
    extra        : dict with additional debug info (epoch counts, trade counts, etc.)
    error        : error string if training crashed, else None
    """
    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)

    now      = datetime.now(timezone.utc)
    ts       = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    run_num  = _get_run_number()

    # ── Verdict ───────────────────────────────────────────────────────────
    OVERFIT_GUARD = 2.0
    any_fold_overfit  = fold_sharpes and any(s > OVERFIT_GUARD for s in fold_sharpes)
    avg_overfit       = avg_sharpe > OVERFIT_GUARD
    fold_spread       = (max(fold_sharpes) - min(fold_sharpes)) if len(fold_sharpes) >= 2 else 0
    inconsistent      = fold_spread > 1.0

    if error:
        verdict = "ERROR"
        verdict_detail = f"Training crashed: {error}"
    elif any_fold_overfit or avg_overfit:
        verdict = "OVERFIT"
        worst_fold = max(fold_sharpes) if fold_sharpes else avg_sharpe
        verdict_detail = (f"Avg {avg_sharpe:.3f} or max fold {worst_fold:.3f} exceeds {OVERFIT_GUARD} — "
                          f"model memorized training data. NOT safe to deploy.")
    elif avg_sharpe >= 1.5 and not inconsistent:
        verdict = "EXCELLENT"
        verdict_detail = "Target range (1.5-2.0), consistent folds. Model is strong."
    elif avg_sharpe >= 1.5 and inconsistent:
        verdict = "UNSTABLE_HIGH"
        verdict_detail = f"High avg but fold spread {fold_spread:.2f} > 1.0 — inconsistent across regimes."
    elif avg_sharpe >= 1.0:
        verdict = "GOOD"
        verdict_detail = "Above go-live threshold (1.0). Solid signal."
    elif avg_sharpe >= 0.5:
        verdict = "WEAK"
        verdict_detail = "Positive but below go-live threshold. Keep accumulating data."
    elif avg_sharpe >= 0.3:
        verdict = "MARGINAL"
        verdict_detail = "Above deployment threshold (0.3) but weak. Rule-based may be better."
    elif avg_sharpe > 0:
        verdict = "BELOW_THRESHOLD"
        verdict_detail = "Positive Sharpe but below 0.3 deployment threshold. Model NOT deployed."
    else:
        verdict = "NEGATIVE"
        verdict_detail = "Negative avg Sharpe. Model has no edge. NOT deployed."

    # Overfitting check
    overfit_flag = ""
    if fold_sharpes:
        max_fold = max(fold_sharpes)
        if max_fold > 2.0:
            overfit_flag = f"  *** OVERFIT WARNING: max fold Sharpe {max_fold:.3f} > 2.0 ***"

    # Consistency check — large spread between folds = unstable
    consistency = ""
    if len(fold_sharpes) >= 2:
        spread = max(fold_sharpes) - min(fold_sharpes)
        if spread > 1.0:
            consistency = f"  *** CONSISTENCY WARNING: fold spread {spread:.3f} > 1.0 — unstable results ***"

    # ── Format report entry ───────────────────────────────────────────────
    sep    = "=" * 72
    subsep = "-" * 72

    lines = [
        "",
        sep,
        f"TRAINING RUN #{run_num}  |  {ts}  |  SOURCE: {source.upper()}",
        sep,
        "",
        f"CONFIG:       {config_name}",
        f"VERDICT:      {verdict}  —  {verdict_detail}",
        f"DEPLOYED:     {'YES — model hot-swapped into production' if deployed else 'NO'}",
        "",
        subsep,
        "SHARPE RESULTS",
        subsep,
        f"Avg Sharpe (real metric):  {avg_sharpe:.4f}",
        f"Fold Sharpes:              {[round(s, 4) for s in fold_sharpes]}",
    ]

    if fold_sharpes:
        lines += [
            f"Best fold:                 {max(fold_sharpes):.4f}",
            f"Worst fold:                {min(fold_sharpes):.4f}",
            f"Fold spread (max-min):     {max(fold_sharpes) - min(fold_sharpes):.4f}",
        ]

    if overfit_flag:
        lines.append(overfit_flag)
    if consistency:
        lines.append(consistency)

    # ── Interpretation guide ──────────────────────────────────────────────
    lines += [
        "",
        subsep,
        "INTERPRETATION",
        subsep,
        "Target avg Sharpe:   1.5 - 2.0   (strong, deployable)",
        "Minimum to deploy:   > 0.3        (any positive edge)",
        "Overfitting flag:    any fold > 2.0",
        "Fold consistency:    spread < 1.0 preferred",
        "",
        "IF avg < 0.3:  Model has no consistent edge. Keep rule-based signals.",
        "              Possible causes:",
        "              - Too few training samples (need more paper trading data)",
        "              - Label threshold too tight or too loose",
        "              - Forward window too short/long for current volatility",
        "",
        "IF folds inconsistent: Model memorizes specific regimes but can't generalise.",
        "              Possible causes:",
        "              - Overfitting to one market period",
        "              - Data too homogeneous (need more regime variety)",
        "",
        "IF overfit warning: Best fold Sharpe above 2.0 is suspicious.",
        "              Could still be real if market was trending strongly.",
        "              Watch live performance closely if deployed.",
    ]

    # ── Data info ─────────────────────────────────────────────────────────
    if data_info:
        lines += [
            "",
            subsep,
            "DATA USED",
            subsep,
        ]
        for k, v in data_info.items():
            lines.append(f"  {k:<28} {v}")

    # ── Extra debug info ──────────────────────────────────────────────────
    if extra:
        lines += [
            "",
            subsep,
            "TRAINING DETAILS",
            subsep,
        ]
        for k, v in extra.items():
            lines.append(f"  {k:<28} {v}")

    # ── Error info ────────────────────────────────────────────────────────
    if error:
        lines += [
            "",
            subsep,
            "ERROR DETAILS",
            subsep,
            error,
        ]

    # ── What to do next ───────────────────────────────────────────────────
    lines += [
        "",
        subsep,
        "NEXT STEPS",
        subsep,
    ]

    if error:
        lines += [
            "  1. Check the error details above",
            "  2. Paste this report to Claude for diagnosis",
        ]
    elif avg_sharpe >= 1.0 and deployed:
        lines += [
            "  1. Monitor live signal quality over next 2-3 trading days",
            "  2. Compare LSTM trade P&L vs rule-based P&L in daily reports",
            "  3. If performance degrades, run: python close_all_positions.py",
            "     then rename model\\lstm_model.pt to lstm_model.pt.bak",
        ]
    elif avg_sharpe >= 0.3 and deployed:
        lines += [
            "  1. Model deployed but marginal — watch performance closely",
            "  2. Run update_training_data.py weekly to add fresh GLD bars",
            "  3. Retrain again after accumulating 50+ more trades",
        ]
    else:
        lines += [
            "  1. Model NOT deployed — rule-based signals continue unchanged",
            "  2. Accumulate more paper trading data (run update_training_data.py weekly)",
            f"  3. Current live trades: {data_info.get('live_trades', 'unknown')} — target 200+ before LSTM will be reliable",
            "  4. Next auto-retrain will fire in ~6 hours (continuous) or run manually",
        ]

    lines.append("")
    lines.append(sep)
    lines.append("")

    report_text = "\n".join(lines)

    # ── Write to file ─────────────────────────────────────────────────────
    try:
        # Trim file if it's gotten too large (keep most recent entries)
        _trim_if_needed()

        with open(REPORT_FILE, "a", encoding="utf-8") as f:
            f.write(report_text)

        print(f"[training_report] Report written to {REPORT_FILE} (run #{run_num})")

    except Exception as e:
        print(f"[training_report] Failed to write report: {e}")


def _get_run_number() -> int:
    """Count existing run entries in the report file."""
    if not os.path.exists(REPORT_FILE):
        return 1
    try:
        with open(REPORT_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        return content.count("TRAINING RUN #") + 1
    except Exception:
        return 1


def _trim_if_needed():
    """If report file is too large, remove oldest entries to keep file manageable."""
    if not os.path.exists(REPORT_FILE):
        return
    size = os.path.getsize(REPORT_FILE)
    if size < MAX_REPORT_BYTES:
        return
    try:
        with open(REPORT_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        # Split by run separator, keep the most recent half
        sep       = "=" * 72
        runs      = content.split(sep)
        keep      = runs[-(len(runs) // 2):]   # keep most recent half
        trimmed   = sep.join(keep)
        with open(REPORT_FILE, "w", encoding="utf-8") as f:
            f.write(f"[Older entries trimmed — keeping most recent {len(keep)//2} runs]\n\n")
            f.write(trimmed)
        print(f"[training_report] File trimmed from {size//1024}KB to {os.path.getsize(REPORT_FILE)//1024}KB")
    except Exception as e:
        print(f"[training_report] Trim failed: {e}")


def read_latest_report(n: int = 1) -> str:
    """
    Return the last N training report entries as a string.
    Useful for pasting to Claude: print(read_latest_report(3))
    """
    if not os.path.exists(REPORT_FILE):
        return "No training reports found yet."
    try:
        with open(REPORT_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        sep  = "=" * 72
        runs = [r for r in content.split(sep) if "TRAINING RUN" in r]
        if not runs:
            return "No complete run entries found in report file."
        last_n = runs[-n:]
        return (sep + "\n").join(last_n)
    except Exception as e:
        return f"Error reading report: {e}"


if __name__ == "__main__":
    # Quick test — write a sample report
    print("Writing test report...")
    write_training_report(
        source       = "test",
        config_name  = "test_config",
        fold_sharpes = [0.42, 0.55, 0.38],
        avg_sharpe   = 0.45,
        deployed     = False,
        data_info    = {"bars_5min": 26000, "live_trades": 23, "timeframe": "5min"},
        extra        = {"epochs_to_converge": [47, 60, 31], "trades_in_val": [120, 145, 98]},
        error        = None
    )
    print(f"\nLatest report entry:")
    print(read_latest_report(1))

