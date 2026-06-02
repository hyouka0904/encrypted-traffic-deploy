#!/bin/bash
# experiment.sh
# 隨機順序跑不同流量，記錄 ground truth log 供事後準確度對照
#
# 用法：
#   bash experiment/experiment.sh <PI_IP> <CLIENT_IP>
#
# 範例：
#   bash experiment/experiment.sh 192.168.4.1 192.168.4.41
#
# 注意：
#   - 需要先在 Pi 上啟動 iperf3 -s 和 flow_monitor
#   - iperf3 流量用 --bind 綁定 CLIENT_IP，確保走 wlan1 出去
#   - VOIP / STREAMING / CHAT 難以用一般工具忠實模擬，故本腳本不包含
#     （VOIP 需 RTP 封包特徵；STREAMING 需持續大流量 + 特定 IAT；
#      CHAT 需短封包 + 低頻率互動，與一般 HTTP 差異微妙）

set -euo pipefail

PI_IP="${1:-192.168.4.1}"
CLIENT_IP="${2:-192.168.4.41}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/../logs"
mkdir -p "$LOG_DIR"

TS=$(date +"%Y%m%d_%H%M%S")
GT_LOG="$LOG_DIR/ground_truth_${TS}.csv"

echo "timestamp,label" > "$GT_LOG"
echo "[exp] ground truth log → $GT_LOG"
echo "[exp] Pi=$PI_IP  Client=$CLIENT_IP"
echo ""

# 流量種類與對應的產生方式
LABELS=("FT" "P2P" "BROWSING")

# 隨機排列（Fisher-Yates）
shuffle() {
    local arr=("$@")
    local n=${#arr[@]}
    for ((i = n - 1; i > 0; i--)); do
        j=$((RANDOM % (i + 1)))
        tmp="${arr[i]}"
        arr[i]="${arr[j]}"
        arr[j]="$tmp"
    done
    echo "${arr[@]}"
}

# 隨機持續時間（30～90秒）
rand_duration() {
    echo $(( RANDOM % 61 + 30 ))
}

# 確保同種流量不連續出現
build_sequence() {
    local shuffled
    shuffled=($(shuffle "${LABELS[@]}"))
    local prev=""
    local seq=()
    for label in "${shuffled[@]}"; do
        if [[ "$label" == "$prev" ]]; then
            # 換一個不同的插到前面
            for alt in "${LABELS[@]}"; do
                if [[ "$alt" != "$label" ]]; then
                    seq+=("$alt")
                    prev="$alt"
                    break
                fi
            done
        fi
        seq+=("$label")
        prev="$label"
    done
    echo "${seq[@]}"
}

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

SEQUENCE=($(build_sequence))
echo "[exp] 實驗順序：${SEQUENCE[*]}"
echo ""

for label in "${SEQUENCE[@]}"; do
    duration=$(rand_duration)
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