"""
Layer 1 unit test — 不需要 scapy / ONNX / Pi
執行方式：python test_flow_monitor.py
"""

import sys
import time
import numpy as np

# ── 把 flow_monitor 的核心類別直接 import（不跑 main）──────────────────
# 假設 test 跟 flow_monitor.py 放在同一個目錄
sys.path.insert(0, ".")

# scapy / onnxruntime / qos_controller 在這裡不需要，先 mock 掉
import unittest.mock as mock
sys.modules.setdefault("scapy",               mock.MagicMock())
sys.modules.setdefault("scapy.all",           mock.MagicMock())
sys.modules.setdefault("onnxruntime",         mock.MagicMock())
sys.modules.setdefault("qos_controller",      mock.MagicMock())

from inference.flow_monitor import FlowRecord, extract_features, IDLE_THRESHOLD_US

# ── 測試用的 feature_cols（與 README 一致）─────────────────────────────
FEATURE_COLS = [
    "duration",
    "flowPktsPerSecond",
    "flowBytesPerSecond",
    "min_flowiat",
    "max_flowiat",
    "mean_flowiat",
    "std_flowiat",
    "min_active",
    "mean_active",
    "max_active",
    "std_active",
    "min_idle",
    "mean_idle",
    "max_idle",
    "std_idle",
]

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

def check(name: str, cond: bool, detail: str = ""):
    status = PASS if cond else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {name}{suffix}")
    return cond

all_passed = True

# ═══════════════════════════════════════════════════════════════════════
print("\n=== Test 1: 封包 < 4 個時回傳 None ===")
flow = FlowRecord(start_us=1000.0 * 1e6)
t = 1000.0  # sec
for i in range(3):
    flow.add_packet(t + i * 0.1, 100, is_forward=True)
result = extract_features(flow, FEATURE_COLS)
all_passed &= check("extract_features 回傳 None", result is None,
                    f"got {result}")

# ═══════════════════════════════════════════════════════════════════════
print("\n=== Test 2: 特徵數量 = 15，順序正確 ===")
flow = FlowRecord(start_us=1000.0 * 1e6)
t = 1000.0
for i in range(10):
    flow.add_packet(t + i * 0.2, 500, is_forward=(i % 2 == 0))
result = extract_features(flow, FEATURE_COLS)
all_passed &= check("result 不是 None", result is not None)
if result is not None:
    all_passed &= check("特徵數量 = 15", len(result) == 15,
                        f"got {len(result)}")

# ═══════════════════════════════════════════════════════════════════════
print("\n=== Test 3: 時間單位是微秒（duration）===")
# 封包從 t=0 到 t=2s，duration 應約為 2_000_000 μs
flow = FlowRecord(start_us=1000.0 * 1e6)
base = 1000.0
for i in range(10):
    flow.add_packet(base + i * 0.2, 100, is_forward=True)  # 共 1.8s
result = extract_features(flow, FEATURE_COLS)
if result is not None:
    duration_val = result[FEATURE_COLS.index("duration")]
    # 應在 1_000_000 ~ 3_000_000 μs 之間
    ok = 1_000_000 < duration_val < 3_000_000
    all_passed &= check("duration 單位是 μs", ok,
                        f"duration={duration_val:.0f} μs")

# ═══════════════════════════════════════════════════════════════════════
print("\n=== Test 4: flowiat 單位是微秒 ===")
# 封包間隔 0.1s = 100_000 μs，mean_flowiat 應約為 100_000
flow = FlowRecord(start_us=2000.0 * 1e6)
base = 2000.0
for i in range(10):
    flow.add_packet(base + i * 0.1, 200, is_forward=True)
result = extract_features(flow, FEATURE_COLS)
if result is not None:
    mean_iat = result[FEATURE_COLS.index("mean_flowiat")]
    ok = 80_000 < mean_iat < 120_000
    all_passed &= check("mean_flowiat 約 100_000 μs", ok,
                        f"mean_flowiat={mean_iat:.0f} μs")

# ═══════════════════════════════════════════════════════════════════════
print("\n=== Test 5: 無 idle 時 idle 欄位是 -1 ===")
# 封包間隔 0.1s，遠小於 IDLE_THRESHOLD（5s），不應產生 idle
flow = FlowRecord(start_us=3000.0 * 1e6)
base = 3000.0
for i in range(10):
    flow.add_packet(base + i * 0.1, 100, is_forward=True)
result = extract_features(flow, FEATURE_COLS)
if result is not None:
    idle_min  = result[FEATURE_COLS.index("min_idle")]
    idle_mean = result[FEATURE_COLS.index("mean_idle")]
    idle_max  = result[FEATURE_COLS.index("max_idle")]
    idle_std  = result[FEATURE_COLS.index("std_idle")]
    all_passed &= check("min_idle  = -1", idle_min  == -1.0, f"got {idle_min}")
    all_passed &= check("mean_idle = -1", idle_mean == -1.0, f"got {idle_mean}")
    all_passed &= check("max_idle  = -1", idle_max  == -1.0, f"got {idle_max}")
    all_passed &= check("std_idle  = -1", idle_std  == -1.0, f"got {idle_std}")

# ═══════════════════════════════════════════════════════════════════════
print("\n=== Test 6: 有 idle 時 idle 欄位有正確值 ===")
# 第一批 5 個封包，間隔 0.1s；然後停 10s（> 5s threshold）；再來 5 個
flow = FlowRecord(start_us=4000.0 * 1e6)
base = 4000.0
for i in range(5):
    flow.add_packet(base + i * 0.1, 100, is_forward=True)
# gap = 10s > IDLE_THRESHOLD
for i in range(5):
    flow.add_packet(base + 10.0 + i * 0.1, 100, is_forward=True)
result = extract_features(flow, FEATURE_COLS)
if result is not None:
    idle_min = result[FEATURE_COLS.index("min_idle")]
    idle_max = result[FEATURE_COLS.index("max_idle")]
    # idle gap 約 10s = 10_000_000 μs
    ok_min = 9_000_000 < idle_min < 11_000_000
    ok_max = 9_000_000 < idle_max < 11_000_000
    all_passed &= check("min_idle 約 10_000_000 μs", ok_min,
                        f"got {idle_min:.0f}")
    all_passed &= check("max_idle 約 10_000_000 μs", ok_max,
                        f"got {idle_max:.0f}")

# ═══════════════════════════════════════════════════════════════════════
print("\n=== Test 7: active 單位是微秒 ===")
# 第一段 active：5 個封包 × 0.1s = 0.4s active；然後 idle 10s；第二段同
flow = FlowRecord(start_us=5000.0 * 1e6)
base = 5000.0
for i in range(5):
    flow.add_packet(base + i * 0.1, 100, is_forward=True)
for i in range(5):
    flow.add_packet(base + 10.0 + i * 0.1, 100, is_forward=True)
result = extract_features(flow, FEATURE_COLS)
if result is not None:
    active_max = result[FEATURE_COLS.index("max_active")]
    # 第一段 active = last_pkt(base+0.4) - active_start(base) = 0.4s = 400_000 μs
    # 第二段 current_active = last_pkt(base+10.4) - active_start(base+10.0) = 0.4s
    ok = 200_000 < active_max < 600_000
    all_passed &= check("max_active 約 400_000 μs", ok,
                        f"got {active_max:.0f}")

# ═══════════════════════════════════════════════════════════════════════
print("\n=== Test 8: rate 欄位維持 per-second（不是 per-μs）===")
# 10 個封包，每個 1000 bytes，duration ≈ 0.9s → flowBytesPerSecond ≈ 10000/0.9
flow = FlowRecord(start_us=6000.0 * 1e6)
base = 6000.0
for i in range(10):
    flow.add_packet(base + i * 0.1, 1000, is_forward=True)
result = extract_features(flow, FEATURE_COLS)
if result is not None:
    bps = result[FEATURE_COLS.index("flowBytesPerSecond")]
    pps = result[FEATURE_COLS.index("flowPktsPerSecond")]
    ok_bps = 5_000 < bps < 20_000
    ok_pps = 5 < pps < 20
    all_passed &= check("flowBytesPerSecond 合理（per-second）", ok_bps,
                        f"got {bps:.1f}")
    all_passed &= check("flowPktsPerSecond 合理（per-second）",  ok_pps,
                        f"got {pps:.1f}")

# ═══════════════════════════════════════════════════════════════════════
print()
if all_passed:
    print("✅  All tests passed.")
else:
    print("❌  Some tests FAILED.")
    sys.exit(1)