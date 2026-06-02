#!/bin/bash
# experiment.sh
# 固定實驗序列，記錄 ground truth log 供事後與推論結果對照計算準確度。
# 三個模型（rf / xgb / lgb）使用同一份序列，各自跑一次，各自評分。
#
# 用法：
#   bash experiment/experiment.sh [PI_IP]
#
# 範例：
#   bash experiment/experiment.sh 192.168.4.1
#
# 注意：
#   - 執行前先在 Pi 上啟動 iperf3 -s 與 flow_monitor
#   - CLIENT_IP 自動偵測本機 192.168.4.x 介面，確保流量走 AP 網段出去
#   - VOIP / STREAMING / CHAT 未納入：VOIP 需 RTP 封包特徵；STREAMING 需
#     持續大流量加特定 IAT；CHAT 需短封包低頻率互動，皆難以忠實模擬

set -euo pipefail

PI_IP="${1:-192.168.4.1}"

# 自動偵測 AP 網段介面 IP（192.168.4.x）
CLIENT_IP=$(ip addr show | awk '/inet 192\.168\.4\./ {print $2}' | cut -d/ -f1 | head -n1)
if [[ -z "$CLIENT_IP" ]]; then
    echo "[exp] 錯誤：找不到 192.168.4.x 介面，請確認已連上 my_rpi_AP"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/../logs"
mkdir -p "$LOG_DIR"

TS=$(date +"%Y%m%d_%H%M%S")
GT_LOG="$LOG_DIR/ground_truth_${TS}.csv"

echo "timestamp,label" > "$GT_LOG"
echo "[exp] ground truth log → $GT_LOG"
echo "[exp] Pi=$PI_IP  Client=$CLIENT_IP"
echo ""

# 固定實驗序列：同種不連續，順序與時間無規律
# 格式：label:duration_sec
SEQUENCE=(
    "BROWSING:45"
    "FT:70"
    "P2P:55"
    "FT:35"
    "BROWSING:80"
    "P2P:40"
    "BROWSING:60"
    "FT:50"
    "P2P:75"
)

run_traffic() {
    local label="$1"
    local duration="$2"
    case "$label" in
        FT)
            # 大流量下行，模擬檔案傳輸
            iperf3 -c "$PI_IP" -t "$duration" -R --bind "$CLIENT_IP" \
                --logfile /dev/null 2>/dev/null || true
            ;;
        P2P)
            # 低頻寬下行，模擬 P2P
            iperf3 -c "$PI_IP" -t "$duration" -R --bind "$CLIENT_IP" \
                -b 2M --logfile /dev/null 2>/dev/null || true
            ;;
        BROWSING)
            # 反覆 HTTP GET，模擬瀏覽行為
            local end=$((SECONDS + duration))
            while [[ $SECONDS -lt $end ]]; do
                curl -s --interface "$CLIENT_IP" \
                    "http://$PI_IP:8080/" -o /dev/null 2>/dev/null || true
                sleep $((RANDOM % 5 + 2))
            done
            ;;
    esac
}

echo "[exp] 實驗序列："
for item in "${SEQUENCE[@]}"; do
    label="${item%%:*}"
    duration="${item##*:}"
    echo "      $label ${duration}s"
done
echo ""

for item in "${SEQUENCE[@]}"; do
    label="${item%%:*}"
    duration="${item##*:}"
    ts=$(date +"%Y-%m-%dT%H:%M:%S")
    echo "$ts,$label" >> "$GT_LOG"
    echo "[exp] ▶ $label  ${duration}s  (開始於 $ts)"
    run_traffic "$label" "$duration"
    echo "[exp] ✓ $label 結束"
    echo ""
done

ts=$(date +"%Y-%m-%dT%H:%M:%S")
echo "$ts,END" >> "$GT_LOG"
echo "[exp] 實驗結束，ground truth log → $GT_LOG"