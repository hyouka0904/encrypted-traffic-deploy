
import argparse
import time
import threading
import numpy as np
from collections import defaultdict
from pathlib import Path

from scapy.all import sniff, IP, TCP, UDP
import onnxruntime as ort

import qos_controller

WINDOW_SEC   = 5      # sec
MODEL_PATH   = Path(__file__).parent.parent / "models" / "model.onnx"
FEATURE_PATH = Path(__file__).parent.parent / "models" / "features.txt"

LABELS = ["BROWSING", "CHAT", "FT", "MAIL", "P2P", "STREAMING", "VOIP"]


# ── Flow Table ────────────────────────────────────────────────────────
class FlowRecord:

    def __init__(self, start_time: float):
        self.start_time   = start_time
        self.last_time    = start_time

        self.fwd_times: list[float] = []
        self.bwd_times: list[float] = []

        # forward / backward bytes
        self.fwd_bytes: list[int] = []
        self.bwd_bytes: list[int] = []

        self.all_times: list[float] = []

        # active / idle interval
        self.active_periods: list[float] = []
        self.idle_periods:   list[float] = []
        self._last_active_start = start_time
        self._last_pkt_time     = start_time
        self.IDLE_THRESHOLD     = 1.0   # sec

    def add_packet(self, ts: float, size: int, is_forward: bool):
        gap = ts - self._last_pkt_time
        if gap > self.IDLE_THRESHOLD:
            self.active_periods.append(self._last_pkt_time - self._last_active_start)
            self.idle_periods.append(gap)
            self._last_active_start = ts

        self._last_pkt_time = ts
        self.last_time = ts
        self.all_times.append(ts)

        if is_forward:
            self.fwd_times.append(ts)
            self.fwd_bytes.append(size)
        else:
            self.bwd_times.append(ts)
            self.bwd_bytes.append(size)

    def close(self):
        # add active period as flow ended
        self.active_periods.append(self._last_pkt_time - self._last_active_start)


def _iat(times: list[float]) -> list[float]:
    # cal inter-arrival times
    if len(times) < 2:
        return [0.0]
    return [times[i] - times[i - 1] for i in range(1, len(times))]


def extract_features(flow: FlowRecord) -> np.ndarray | None:

    if len(flow.all_times) < 4:
        return None  

    flow.close()
    duration = flow.last_time - flow.start_time
    if duration <= 0:
        return None

    fiat = _iat(flow.fwd_times)
    biat = _iat(flow.bwd_times)
    flowiat = _iat(flow.all_times)

    total_fiat = float(np.sum(fiat))
    total_biat = float(np.sum(biat))

    active = flow.active_periods if flow.active_periods else [0.0]
    idle   = flow.idle_periods   if flow.idle_periods   else [0.0]

    n_pkts  = len(flow.all_times)
    n_bytes = sum(flow.fwd_bytes) + sum(flow.bwd_bytes)

    features = [
        duration,
        total_fiat,
        total_biat,
        float(np.min(fiat)),
        float(np.min(biat)),
        float(np.max(fiat)),
        float(np.max(biat)),
        float(np.mean(fiat)),
        float(np.mean(biat)),
        n_pkts  / duration,           # flowPktsPerSecond
        n_bytes / duration,           # flowBytesPerSecond
        float(np.min(flowiat)),
        float(np.max(flowiat)),
        float(np.mean(flowiat)),
        float(np.std(flowiat)),
        float(np.min(active)),
        float(np.mean(active)),
        float(np.max(active)),
        float(np.std(active)),
        float(np.min(idle)),
        float(np.mean(idle)),
        float(np.max(idle)),
        float(np.std(idle)),
    ]
    return np.array(features, dtype=np.float32)


class FlowMonitor:
    def __init__(self, iface: str):
        self.iface   = iface
        self.session = ort.InferenceSession(str(MODEL_PATH))
        self.input_name = self.session.get_inputs()[0].name

        self.flow_table: dict[tuple, FlowRecord] = {}
        self.lock = threading.Lock()

        self.infer_thread = threading.Thread(target=self._infer_loop, daemon=True)
        self.infer_thread.start()

        print(f"[monitor] : {iface}  {WINDOW_SEC}s")
        print(f"[monitor] model: {MODEL_PATH}")

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

        # forward key = (src, dst, sp, dp, proto)
        # backward key = (dst, src, dp, sp, proto)
        rev_key = (key[1], key[0], key[3], key[2], key[4])
        ts   = time.time()
        size = len(pkt)

        with self.lock:
            if key in self.flow_table:
                self.flow_table[key].add_packet(ts, size, is_forward=True)
            elif rev_key in self.flow_table:
                self.flow_table[rev_key].add_packet(ts, size, is_forward=False)
            else:
                record = FlowRecord(ts)
                record.add_packet(ts, size, is_forward=True)
                self.flow_table[key] = record

    def _infer_loop(self):
        while True:
            time.sleep(WINDOW_SEC)
            self._run_inference()

    def _run_inference(self):
        with self.lock:
            keys    = list(self.flow_table.keys())
            records = list(self.flow_table.values())

        results = []
        for key, flow in zip(keys, records):
            feat = extract_features(flow)
            if feat is None:
                continue
            x = feat.reshape(1, -1)
            pred = self.session.run(None, {self.input_name: x})
            label = pred[0][0]   # string label
            src_ip = key[0]
            results.append((src_ip, label))
            print(f"[infer] {src_ip:<16} → {label}")

        if results:
            qos_controller.apply_batch(results)

        # clear flow without package over 60s
        now = time.time()
        with self.lock:
            stale = [k for k, v in self.flow_table.items()
                     if now - v.last_time > 60]
            for k in stale:
                del self.flow_table[k]

    def start(self):
        sniff(iface=self.iface, prn=self.packet_callback, store=False)



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iface", default="wlan1", help="")
    args = parser.parse_args()

    monitor = FlowMonitor(args.iface)
    monitor.start()


if __name__ == "__main__":
    main()