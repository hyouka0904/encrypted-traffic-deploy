import argparse
import sys
import time
import threading
import numpy as np
import psutil
from pathlib import Path

from scapy.all import sniff, IP, TCP, UDP
import onnxruntime as ort

import qos_controller

TICK_SEC          = 5
WINDOW_SEC        = 15
IDLE_THRESHOLD_US = 5_000_000

MODELS_DIR   = Path(__file__).parent.parent / "models"
FEATURE_PATH = MODELS_DIR / "features.txt"

STABLE_N   = 3   # 連續幾次相同才改 mark
SUMMARY_N  = 10  # 每幾個 tick 印一次統計摘要


def _resolve_model(name: str) -> Path:
    path = MODELS_DIR / f"{name}.onnx"
    if path.exists():
        return path
    available = sorted(p.stem for p in MODELS_DIR.glob("*.onnx"))
    print(f"[error] 找不到模型：{path}")
    if available:
        print(f"[error] models/ 內可用的模型：{', '.join(available)}")
    else:
        print(f"[error] models/ 內沒有任何 .onnx 檔案")
    sys.exit(1)


def _load_features(path: Path) -> list[str]:
    with open(path) as f:
        return [line.strip() for line in f if line.strip()]


def _load_label_classes(model_path: Path) -> list[str] | None:
    path = model_path.parent / "label_classes.txt"
    if not path.exists():
        return None
    with open(path) as f:
        return [line.strip() for line in f if line.strip()]


# ── Flow Table ────────────────────────────────────────────────────────
class FlowRecord:
    def __init__(self, start_us: float):
        self.start_us = start_us
        self.last_us  = start_us
        self.all_times: list[float] = []
        self.fwd_bytes: list[int] = []
        self.bwd_bytes: list[int] = []
        self.active_periods: list[float] = []
        self.idle_periods:   list[float] = []
        self._last_active_start = start_us
        self._last_pkt_us       = start_us

    def add_packet(self, ts_sec: float, size: int, is_forward: bool):
        ts_us  = ts_sec * 1e6
        gap_us = ts_us - self._last_pkt_us
        if gap_us > IDLE_THRESHOLD_US:
            active_dur = self._last_pkt_us - self._last_active_start
            self.active_periods.append(active_dur)
            self.idle_periods.append(gap_us)
            self._last_active_start = ts_us
        self._last_pkt_us = ts_us
        self.last_us      = ts_us
        self.all_times.append(ts_us)
        if is_forward:
            self.fwd_bytes.append(size)
        else:
            self.bwd_bytes.append(size)

    def trim_window(self, now_sec: float):
        cutoff_us = (now_sec - WINDOW_SEC) * 1e6
        if not self.all_times or self.all_times[-1] < cutoff_us:
            return
        idx = 0
        while idx < len(self.all_times) and self.all_times[idx] < cutoff_us:
            idx += 1
        if idx:
            self.all_times = self.all_times[idx:]


def _iat(times: list[float]) -> list[float]:
    if len(times) < 2:
        return []
    return [times[i] - times[i - 1] for i in range(1, len(times))]


def _stats(values: list[float], sentinel: float = -1.0) -> tuple[float, float, float, float]:
    if not values:
        return sentinel, sentinel, sentinel, sentinel
    arr = np.array(values, dtype=np.float64)
    return float(arr.min()), float(arr.mean()), float(arr.max()), float(arr.std())


def extract_features(flow: "FlowRecord", feature_cols: list[str]) -> "np.ndarray | None":
    if len(flow.all_times) < 4:
        return None
    duration_us = flow.last_us - flow.start_us
    if duration_us <= 0:
        return None
    duration_sec = duration_us / 1e6
    flowiat = _iat(flow.all_times)
    n_pkts  = len(flow.all_times)
    n_bytes = sum(flow.fwd_bytes) + sum(flow.bwd_bytes)
    current_active = flow._last_pkt_us - flow._last_active_start
    active = flow.active_periods + ([current_active] if current_active > 0 else [])
    idle   = flow.idle_periods
    flowiat_min, flowiat_mean, flowiat_max, flowiat_std = _stats(flowiat)
    active_min, active_mean, active_max, active_std     = _stats(active)
    idle_min,   idle_mean,   idle_max,   idle_std       = _stats(idle)
    feature_map = {
        "duration":           duration_us,
        "flowPktsPerSecond":  n_pkts  / duration_sec,
        "flowBytesPerSecond": n_bytes / duration_sec,
        "min_flowiat":        flowiat_min,
        "max_flowiat":        flowiat_max,
        "mean_flowiat":       flowiat_mean,
        "std_flowiat":        flowiat_std,
        "min_active":         active_min,
        "mean_active":        active_mean,
        "max_active":         active_max,
        "std_active":         active_std,
        "min_idle":           idle_min,
        "mean_idle":          idle_mean,
        "max_idle":           idle_max,
        "std_idle":           idle_std,
    }
    try:
        vec = [feature_map[col] for col in feature_cols]
    except KeyError as e:
        raise RuntimeError(f"features.txt 含未知欄位：{e}")
    return np.array(vec, dtype=np.float32)


# ── Monitor ───────────────────────────────────────────────────────────
class FlowMonitor:
    def __init__(self, iface: str, model_path: Path, ground_truth: str | None):
        self.iface         = iface
        self.ground_truth  = ground_truth   # 當前 app 類別，None 表示不量準確度
        self.feature_cols  = _load_features(FEATURE_PATH)

        self.ip_label_history: dict[str, list[str]] = {}

        self.session    = ort.InferenceSession(str(model_path))
        self.input_name = self.session.get_inputs()[0].name
        self.label_classes = _load_label_classes(model_path)

        self.flow_table: dict[tuple, FlowRecord] = {}
        self.lock = threading.Lock()

        # ── 統計資料 ──
        self.infer_latencies: list[float] = []   # 每次 session.run 耗時（ms）
        self.label_counts:    dict[str, int] = {}
        self.tick_count       = 0
        self.correct_ticks    = 0  # ground truth 吻合的 tick 數
        self.total_ticks      = 0  # 有產生 stable label 的 tick 數

        self._proc = psutil.Process()

        self.infer_thread = threading.Thread(target=self._infer_loop, daemon=True)
        self.infer_thread.start()

        print(f"[monitor] iface={iface}  tick={TICK_SEC}s  window={WINDOW_SEC}s")
        print(f"[monitor] model:    {model_path}")
        print(f"[monitor] features: {self.feature_cols}")
        if self.label_classes:
            print(f"[monitor] label_classes: {self.label_classes}")
        else:
            print(f"[monitor] label_classes: 模型自帶字串輸出")
        if self.ground_truth:
            print(f"[monitor] ground_truth: {self.ground_truth}")

    def _make_key(self, pkt) -> tuple | None:
        if not pkt.haslayer(IP):
            return None
        ip = pkt[IP]
        if pkt.haslayer(TCP):
            t = pkt[TCP]
            return (ip.src, ip.dst, t.sport, t.dport, 6)
        if pkt.haslayer(UDP):
            u = pkt[UDP]
            return (ip.src, ip.dst, u.sport, u.dport, 17)
        return None

    def packet_callback(self, pkt):
        key = self._make_key(pkt)
        if key is None:
            return
        rev_key = (key[1], key[0], key[3], key[2], key[4])
        ts   = time.time()
        size = len(pkt)
        with self.lock:
            if key in self.flow_table:
                self.flow_table[key].add_packet(ts, size, is_forward=True)
            elif rev_key in self.flow_table:
                self.flow_table[rev_key].add_packet(ts, size, is_forward=False)
            else:
                record = FlowRecord(ts * 1e6)
                record.add_packet(ts, size, is_forward=True)
                self.flow_table[key] = record

    def _infer_loop(self):
        while True:
            time.sleep(TICK_SEC)
            self._run_inference()

    def _run_inference(self):
        now = time.time()
        self.tick_count += 1

        with self.lock:
            keys    = list(self.flow_table.keys())
            records = list(self.flow_table.values())

        # CPU / RAM（推論前量一次）
        cpu_pct = self._proc.cpu_percent(interval=None)
        mem_mb  = self._proc.memory_info().rss / 1024 / 1024

        # per-IP 收集推論結果，同時計時
        ip_votes: dict[str, list[str]] = {}
        for key, flow in zip(keys, records):
            flow.trim_window(now)
            feat = extract_features(flow, self.feature_cols)
            if feat is None:
                continue
            x = feat.reshape(1, -1)

            t0    = time.perf_counter()
            pred  = self.session.run(None, {self.input_name: x})
            t1    = time.perf_counter()
            self.infer_latencies.append((t1 - t0) * 1000)  # ms

            raw = pred[0][0]
            label = self.label_classes[int(raw)] if self.label_classes is not None else str(raw)
            src_ip = key[0]
            ip_votes.setdefault(src_ip, []).append(label)
            print(f"[infer] {src_ip:<16} → {label}  ({(t1-t0)*1000:.2f}ms)")

        # 多數決
        results = []
        for ip, votes in ip_votes.items():
            winner = max(set(votes), key=votes.count)
            if len(votes) > 1:
                print(f"[vote]  {ip:<16} votes={votes} → {winner}")
            results.append((ip, winner))

        # 穩定機制 + 準確度統計
        stable = []
        for ip, label in results:
            self.label_counts[label] = self.label_counts.get(label, 0) + 1
            history = self.ip_label_history.setdefault(ip, [])
            history.append(label)
            if len(history) > STABLE_N:
                history.pop(0)
            if len(history) == STABLE_N and len(set(history)) == 1:
                stable.append((ip, label))
                print(f"[stable] {ip:<16} → {label} (連續 {STABLE_N} 次)")
                if self.ground_truth is not None:
                    self.total_ticks += 1
                    if label == self.ground_truth:
                        self.correct_ticks += 1

        if stable:
            qos_controller.apply_batch(stable)

        # 清除 stale flow
        with self.lock:
            stale = [k for k, v in self.flow_table.items()
                     if now - v.last_us / 1e6 > 60]
            for k in stale:
                del self.flow_table[k]

        # 每 SUMMARY_N 個 tick 印統計摘要
        if self.tick_count % SUMMARY_N == 0:
            self._print_summary(cpu_pct, mem_mb)

    def _print_summary(self, cpu_pct: float, mem_mb: float):
        print(f"\n{'='*50}")
        print(f"[summary] tick #{self.tick_count}")

        # 推論耗時
        if self.infer_latencies:
            arr = np.array(self.infer_latencies)
            print(f"[summary] 推論耗時(ms)  mean={arr.mean():.2f}  "
                  f"max={arr.max():.2f}  std={arr.std():.2f}  "
                  f"n={len(arr)}")
        else:
            print(f"[summary] 推論耗時：尚無資料")

        # CPU / RAM
        print(f"[summary] CPU={cpu_pct:.1f}%  RAM={mem_mb:.1f}MB")

        # label 分布
        total = sum(self.label_counts.values())
        if total:
            dist = {k: f"{v}({v/total*100:.0f}%)" for k, v in
                    sorted(self.label_counts.items(), key=lambda x: -x[1])}
            print(f"[summary] label分布: {dist}")

        # 準確度
        if self.ground_truth is not None:
            if self.total_ticks:
                acc = self.correct_ticks / self.total_ticks * 100
                print(f"[summary] 準確度: {self.correct_ticks}/{self.total_ticks} = {acc:.1f}%  "
                      f"(ground_truth={self.ground_truth})")
            else:
                print(f"[summary] 準確度：尚無 stable tick")

        print(f"{'='*50}\n")

    def start(self):
        sniff(iface=self.iface, prn=self.packet_callback, store=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iface", default="wlan1",
                        help="監聽的網路介面")
    parser.add_argument("--model", default="rf",
                        help="模型名稱（不含 .onnx），預設 rf")
    parser.add_argument("--ground-truth", default=None,
                        choices=["VOIP", "STREAMING", "CHAT", "BROWSING",
                                 "MAIL", "FT", "P2P"],
                        help="當前流量的真實類別，用於準確度統計")
    args = parser.parse_args()

    model_path = _resolve_model(args.model)
    monitor    = FlowMonitor(args.iface, model_path, args.ground_truth)
    monitor.start()


if __name__ == "__main__":
    main()