#!/usr/bin/env python3
"""
eval.py
將 flow_monitor 產生的 infer log 與 ground_truth.csv 以 offset_sec 對齊，
計算分類準確度，輸出 results/result_<model>_<timestamp>.json。

用法：
    python experiment/eval.py --infer logs/infer_rf_20240101_120000.csv
    python experiment/eval.py --infer logs/infer_xgb_20240101_120000.csv --gt experiment/ground_truth.csv
"""

import argparse
import csv
import json
import re
from pathlib import Path

BASE_DIR    = Path(__file__).parent.parent
DEFAULT_GT  = Path(__file__).parent / "ground_truth.csv"
RESULTS_DIR = BASE_DIR / "results"


# ── 讀檔 ──────────────────────────────────────────────────────────────

def load_infer(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    # offset_sec 轉 float
    for r in rows:
        r["offset_sec"] = float(r["offset_sec"])
    return rows


def load_ground_truth(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["offset_sec"] = float(r["offset_sec"])
    return rows


# ── offset 對齊 ───────────────────────────────────────────────────────

def build_intervals(gt_rows: list[dict]) -> list[tuple[float, float, str]]:
    """
    回傳 [(start_offset, end_offset, label), ...]
    END 標記不產生區間，只當作前一段的結束點。
    """
    intervals = []
    for i, row in enumerate(gt_rows):
        if row["label"] == "END":
            break
        start = row["offset_sec"]
        end   = gt_rows[i + 1]["offset_sec"]  # 下一筆一定存在（最後是 END）
        intervals.append((start, end, row["label"]))
    return intervals


def assign_ground_truth(infer_rows: list[dict],
                        intervals: list[tuple[float, float, str]]) -> list[dict]:
    """
    每筆推論的 offset_sec 落在哪個區間就給對應 label。
    不在任何區間內的筆數跳過。
    """
    results = []
    for row in infer_rows:
        offset = row["offset_sec"]
        gt = None
        for start, end, label in intervals:
            if start <= offset < end:
                gt = label
                break
        if gt is None:
            continue
        results.append({
            "offset_sec":   offset,
            "ip":           row["ip"],
            "predicted":    row["label"],
            "ground_truth": gt,
            "correct":      row["label"] == gt,
        })
    return results


# ── 準確度計算 ────────────────────────────────────────────────────────

def compute_metrics(rows: list[dict]) -> dict:
    labels  = sorted(set(r["ground_truth"] for r in rows))
    total   = len(rows)
    correct = sum(1 for r in rows if r["correct"])

    per_label = {}
    for label in labels:
        subset    = [r for r in rows if r["ground_truth"] == label]
        n         = len(subset)
        n_correct = sum(1 for r in subset if r["correct"])
        predicted_as: dict[str, int] = {}
        for r in subset:
            predicted_as[r["predicted"]] = predicted_as.get(r["predicted"], 0) + 1
        per_label[label] = {
            "total":        n,
            "correct":      n_correct,
            "accuracy":     round(n_correct / n * 100, 1) if n else 0.0,
            "predicted_as": predicted_as,
        }

    return {
        "total":            total,
        "correct":          correct,
        "overall_accuracy": round(correct / total * 100, 1) if total else 0.0,
        "per_label":        per_label,
    }


# ── 從檔名解析 model / timestamp ──────────────────────────────────────

def parse_infer_filename(path: Path) -> tuple[str, str]:
    """
    infer_<model>_<timestamp>.csv → (model, timestamp)
    e.g. infer_rf_20240101_120000.csv → ("rf", "20240101_120000")
    """
    m = re.match(r"infer_(.+?)_(\d{8}_\d{6})\.csv$", path.name)
    if m:
        return m.group(1), m.group(2)
    # fallback
    return "unknown", path.stem


# ── main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--infer", required=True,
                        help="infer log CSV（logs/infer_<model>_<timestamp>.csv）")
    parser.add_argument("--gt", default=str(DEFAULT_GT),
                        help=f"ground truth CSV（預設：{DEFAULT_GT}）")
    args = parser.parse_args()

    infer_path = Path(args.infer)
    gt_path    = Path(args.gt)

    infer_rows = load_infer(infer_path)
    gt_rows    = load_ground_truth(gt_path)

    intervals = build_intervals(gt_rows)
    matched   = assign_ground_truth(infer_rows, intervals)

    if not matched:
        print("[eval] 沒有找到區間內的推論結果，請確認 infer log 的 offset_sec 範圍與 ground truth 一致")
        return

    metrics   = compute_metrics(matched)
    model, ts = parse_infer_filename(infer_path)

    # ground truth 序列（不含 END）
    gt_sequence = [
        {"offset_sec": r["offset_sec"], "label": r["label"]}
        for r in gt_rows if r["label"] != "END"
    ]

    output = {
        # 1. 準確度統計
        "metrics": metrics,
        # 2. 逐筆推論明細
        "details": matched,
        # 3. 使用的 ground truth 序列
        "ground_truth": gt_sequence,
        # metadata
        "model":     model,
        "timestamp": ts,
        "infer_log": str(infer_path),
        "gt_file":   str(gt_path),
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"result_{model}_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"[eval] 結果寫入 → {out_path}")
    print(f"[eval] overall accuracy: {metrics['overall_accuracy']}%  "
          f"({metrics['correct']}/{metrics['total']} ticks)")
    for label, stat in metrics["per_label"].items():
        print(f"[eval]   {label:<12} {stat['accuracy']:5.1f}%  "
              f"({stat['correct']}/{stat['total']})  "
              f"predicted_as={stat['predicted_as']}")


if __name__ == "__main__":
    main()