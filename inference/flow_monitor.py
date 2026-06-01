import argparse
import sys
import time
import threading
import numpy as np
from pathlib import Path

from scapy.all import sniff, IP, TCP, UDP
import onnxruntime as ort

import qos_controller

TICK_SEC          = 5          # 每幾秒做一次推論
WINDOW_SEC        = 15         # sliding window 長度（秒）
IDLE_THRESHOLD_US = 5_000_000  # 5s，單位微秒

MODELS_DIR   = Path(__file__).parent.parent / "models"
FEATURE_PATH = MODELS_DIR / "features.txt"


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
    """載入與模型同目錄的 label_classes.txt，找不到時回傳 None（代表模型自己輸出字串）。"""
    path = model_path.parent / "label_classes.txt"
    if not path.exists():
        return None
    with open(path) as f:
        return [line.strip() for line in f if line.strip()]


# ── Flow Table ────────────────────────────────────────────────────────
class FlowRecord:
    """
    所有時間單位：微秒（μs）。
    封包時間戳記在 add_packet 時就轉成 μs 存入。
    """

    def __init__(self, start_us: float):
        self.start_us = start_us
        self.last_us  = start_us

        self.all_times: list[float] = []  # μs，用於 flowiat / duration / window 裁切

        self.fwd_bytes: list[int] = []
        self.bwd_bytes: list[int] = []

        # active / idle
        self.active_periods: list[float] = []  # μs
        self.idle_periods:   list[float] = []  # μs
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
        """丟掉 window 以外的舊封包（保留最近 WINDOW_SEC 秒）。
        注意：fwd/bwd_bytes 與 active/idle_periods 無對應時間戳，不裁切，
        rate 與 active/idle 統計為整條 flow 歷史的近似值。
        """
        cutoff_us = (now_sec - WINDOW_SEC) * 1e6
        if not self.all_times or self.all_times[-1] < cutoff_us:
            return
        idx = 0
        while idx < len(self.all_times) and self.all_times[idx] < cutoff_us:
            idx += 1
        if idx:
            self.all_times = self.all_times[idx:]


def _iat(times: list[float]) -> list[float]:
    """計算 inter-arrival time 列表（μs）。"""
    if len(times) < 2:
        return []
    return [times[i] - times[i - 1] for i in range(1, len(times))]


def _stats(values: list[float], sentinel: float = -1.0) -> tuple[float, float, float, float]:
    """回傳 (min, mean, max, std)，無資料時填 sentinel。"""
    if not values:
        return sentinel, sentinel, sentinel, sentinel
    arr = np.array(values, dtype=np.float64)
    return float(arr.min()), float(arr.mean()), float(arr.max()), float(arr.std())


def extract_features(flow: "FlowRecord", feature_cols: list[str]) -> "np.ndarray | None":
    """
    依 feature_cols 順序組出特徵向量。
    時間單位：μs（duration、flowiat、active、idle）。
    rate 兩欄（flowPktsPerSecond、flowBytesPerSecond）維持 per-second。
    """
    if len(flow.all_times) < 4:
        return None

    duration_us = flow.last_us - flow.start_us
    if duration_us <= 0:
        return None
    duration_sec = duration_us / 1e6

    flowiat = _iat(flow.all_times)  # μs

    n_pkts  = len(flow.all_times)
    n_bytes = sum(flow.fwd_bytes) + sum(flow.bwd_bytes)

    # active：加上目前正在 active 的這段（flow 還活著，不呼叫 close）
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
    def __init__(self, iface: str, model_path: Path):
        self.iface        = iface
        self.feature_cols = _load_features(FEATURE_PATH)

        self.ip_label_history: dict[str, list[str]] = {}

        self.session    = ort.InferenceSession(str(model_path))
        self.input_name = self.session.get_inputs()[0].name
        self.label_classes = _load_label_classes(model_path)

        self.flow_table: dict[tuple, FlowRecord] = {}
        self.lock = threading.Lock()

        self.infer_thread = threading.Thread(target=self._infer_loop, daemon=True)
        self.infer_thread.start()

        print(f"[monitor] iface={iface}  tick={TICK_SEC}s  window={WINDOW_SEC}s")
        print(f"[monitor] model:    {model_path}")
        print(f"[monitor] features: {self.feature_cols}")
        if self.label_classes:
            print(f"[monitor] label_classes: {self.label_classes}")
        else:
            print(f"[monitor] label_classes: 模型自帶字串輸出")

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

        with self.lock:
            keys    = list(self.flow_table.keys())
            records = list(self.flow_table.values())

        # per-IP 收集所有 flow 的推論結果
        ip_votes: dict[str, list[str]] = {}
        for key, flow in zip(keys, records):
            flow.trim_window(now)
            feat = extract_features(flow, self.feature_cols)
            if feat is None:
                continue
            x     = feat.reshape(1, -1)
            pred  = self.session.run(None, {self.input_name: x})
            raw = pred[0][0]
            if self.label_classes is not None:
                label = self.label_classes[int(raw)]  # XGB / LGB 輸出 int index
            else:
                label = str(raw) 
            src_ip = key[0]
            ip_votes.setdefault(src_ip, []).append(label)
            print(f"[infer] {src_ip:<16} → {label}")

        # 多數決：同一 IP 有多條 flow 時取票數最多的 label
        results = []
        for ip, votes in ip_votes.items():
            winner = max(set(votes), key=votes.count)
            if len(votes) > 1:
                print(f"[vote]  {ip:<16} votes={votes} → {winner}")
            results.append((ip, winner))

        STABLE_N = 3  # 連續幾次相同才改 mark

        stable = []
        for ip, label in results:
            history = self.ip_label_history.setdefault(ip, [])
            history.append(label)
            if len(history) > STABLE_N:
                history.pop(0)
            if len(history) == STABLE_N and len(set(history)) == 1:
                stable.append((ip, label))
                print(f"[stable] {ip:<16} → {label} (連續 {STABLE_N} 次)")

        if stable:
            qos_controller.apply_batch(stable)

        # 清除超過 60s 沒有封包的 stale flow
        with self.lock:
            stale = [k for k, v in self.flow_table.items()
                     if now - v.last_us / 1e6 > 60]
            for k in stale:
                del self.flow_table[k]

    def start(self):
        sniff(iface=self.iface, prn=self.packet_callback, store=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iface",  default="wlan1",
                        help="監聽的網路介面")
    parser.add_argument("--model",  default="rf",
                        help="模型名稱（不含 .onnx），預設 rf")
    args = parser.parse_args()

    model_path = _resolve_model(args.model)
    monitor    = FlowMonitor(args.iface, model_path)
    monitor.start()


if __name__ == "__main__":
    main()