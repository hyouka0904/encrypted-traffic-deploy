#!/usr/bin/env python3
"""
eval.py
讀取 flow_monitor 的推論 log 和 experiment.sh 的 ground truth log，
按時間戳對齊後計算各 label 的準確度，輸出 JSON。

用法：
    python experiment/eval.py \\
        --infer  logs/infer_rf_20240101_120000.csv \\
        --gt     logs/ground_truth_20240101_120000.csv \\
        --out    logs/eval_rf_20240101.json
"""

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path


def parse_csv(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")


def assign_ground_truth(infer_rows: list[dict], gt_rows: list[dict]) -> list[dict]:
    """
    對每筆推論結果，根據時間戳找到對應的 ground truth label。
    gt_rows 格式：[{timestamp, label}, ...]，label="END" 表示實驗結束。
    """
    # 建立 (開始時間, 結束時間, label) 區間列表
    intervals = []
    for i, row in enumerate(gt_rows):
        if row["label"] == "END":
            break
        start = parse_ts(row["timestamp"])
        if i + 1 < len(gt_rows):
            end = parse_ts(gt_rows[i + 1]["timestamp"])
        else:
            end = None
        intervals.append((start, end, row["label"]))

    results = []
    for row in infer_rows:
        ts = parse_ts(row["timestamp"])
        gt = None
        for start, end, label in intervals:
            if ts >= start and (end is None or ts < end):
                gt = label
                break
        if gt is None:
            continue  # 推論時間不在實驗區間內，跳過
        results.append({
            "timestamp": row["timestamp"],
            "ip":        row["ip"],
            "predicted": row["label"],
            "ground_truth": gt,
            "correct":   row["label"] == gt,
        })
    return results


def compute_metrics(rows: list[dict]) -> dict:
    labels = sorted(set(r["ground_truth"] for r in rows))
    total  = len(rows)
    correct = sum(1 for r in rows if r["correct"])

    per_label = {}
    for label in labels:
        subset   = [r for r in rows if r["ground_truth"] == label]
        n        = len(subset)
        n_correct = sum(1 for r in subset if r["correct"])
        predicted_as = {}
        for r in subset:
            predicted_as[r["predicted"]] = predicted_as.get(r["predicted"], 0) + 1
        per_label[label] = {
            "total":       n,
            "correct":     n_correct,
            "accuracy":    round(n_correct / n * 100, 1) if n else 0,
            "predicted_as": predicted_as,
        }

    return {
        "total":          total,
        "correct":        correct,
        "overall_accuracy": round(correct / total * 100, 1) if total else 0,
        "per_label":      per_label,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--infer", required=True,
                        help="flow_monitor 輸出的推論 CSV")
    parser.add_argument("--gt",    required=True,
                        help="experiment.sh 輸出的 ground truth CSV")
    parser.add_argument("--out",   default=None,
                        help="輸出 JSON 路徑，預設印到 stdout")
    args = parser.parse_args()

    infer_rows = parse_csv(Path(args.infer))
    gt_rows    = parse_csv(Path(args.gt))

    matched = assign_ground_truth(infer_rows, gt_rows)
    if not matched:
        print("[eval] 沒有找到時間區間內的推論結果，請確認兩份 log 的時間範圍重疊")
        return

    metrics = compute_metrics(matched)

    output = {
        "infer_log":    args.infer,
        "gt_log":       args.gt,
        "matched_ticks": len(matched),
        "metrics":      metrics,
    }

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"[eval] 結果寫入 → {out_path}")
    else:
        print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()