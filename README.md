# encrypted-traffic-deploy

Raspberry Pi 5 AP 部署專案。以 Edimax USB 網卡（wlan1）開 WiFi AP，對連入裝置實施 Captive Portal 強制登入，並以 ONNX Runtime 在 Pi 上執行加密流量分類模型，依推論結果透過 tc HTB 套用 QoS 限速。

**分類模型訓練專案**：[encrypted-traffic-train](https://github.com/hyouka0904/encrypted-traffic-train)（ISCX-VPN 2016 Scenario B）

---

## 系統架構

```
client 連上 my_rpi_AP
        │
        ▼
Captive Portal（Flask, portal/app.py）
  └─ 未登入 → 攔截導向登入頁
  └─ 登入後 → iptables 白名單放行上網
  └─ 登入後 → status 頁面可一鍵跑流量分類實驗
        │
        ▼
flow_monitor（inference/flow_monitor.py）
  └─ scapy 即時抓 wlan1 封包 → 抽 15 特徵 → ONNX Runtime 推論
  └─ 每個 stable tick 寫入 logs/flow_infer_log.csv
        │
        ▼
qos_controller（inference/qos_controller.py）
  └─ iptables mangle POSTROUTING 打 fwmark → tc HTB 依 class 限速（下行，wlan1 egress）
```

---

## 目錄結構

```
encrypted-traffic-deploy/
├── portal/
│   ├── app.py                 # Flask Captive Portal（含實驗 start/stop endpoint）
│   ├── portal.db              # SQLite（自動建立）
│   ├── setup_iptables.sh      # iptables 攔截規則初始化
│   └── templates/
│       ├── portal.html
│       ├── login.html
│       ├── register.html
│       ├── register_success.html
│       ├── status.html        # 登入後頁面，含一鍵實驗按鈕
│       ├── admin_login.html
│       └── admin_dashboard.html
├── inference/
│   ├── flow_monitor.py        # 即時封包抓取、特徵萃取、推論，寫 flow_infer_log.csv
│   └── qos_controller.py      # tc HTB 初始化與 iptables fwmark 控制
├── experiment/
│   ├── ground_truth.csv       # 預先產生的固定實驗序列（offset_sec,label）
│   └── eval.py                # 推論結果與 ground truth 對照，計算準確度
├── models/
│   └── features.txt           # 特徵欄位順序（與訓練端一致，15 欄）
├── configs/
│   └── qos_policy.yaml        # 各流量 class 的頻寬設定
├── logs/
│   └── .gitkeep               # flow_infer_log.csv 與 infer_*.csv 皆不推 git
├── results/
│   └── .gitkeep               # eval.py 輸出的 result_*.json（推 git）
└── requirements.txt
```

---

## 安裝套件

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Portal 使用 `sudo` 執行，需另外為 root 安裝 Flask：

```bash
sudo python3 -m pip install flask --break-system-packages
```

---

## 環境變數設定

啟動前必須設定以下環境變數（建議寫入 `/etc/environment` 或 systemd unit 的 `Environment=`）：

| 變數 | 說明 |
|------|------|
| `FLASK_SECRET_KEY` | Session 加密金鑰，請設為隨機長字串 |
| `ADMIN_USER` | Admin 帳號名稱（預設 `admin`） |
| `ADMIN_PASSWORD_HASH` | Admin 密碼的 Werkzeug hash（見下方產生方式） |

**產生 Admin 密碼 Hash：**

```bash
python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('你的密碼'))"
```

---

## Raspberry Pi AP 設定

**裝置**：Raspberry Pi 5 (8GB)，hostname: `MadChicken`

### 網路介面配置

| 介面 | 硬體 | 角色 |
|------|------|------|
| `wlan0` | 內建 WiFi | 連接家裡 WiFi（上網用） |
| `wlan1` | Edimax N150（RTL8188EU, `7392:b811`） | AP（`my_rpi_AP`） |

---

### 步驟一：更換 wlan1 驅動

```bash
sudo apt install -y git build-essential dkms bc linux-headers-$(uname -r)
git clone https://github.com/aircrack-ng/rtl8188eus.git
cd rtl8188eus
echo "blacklist rtl8xxxu" | sudo tee /etc/modprobe.d/rtl8188eus.conf
sudo make
sudo make install
sudo modprobe -r rtl8xxxu
sudo modprobe 8188eu
```

> RTL8188EU 驅動在 AP 模式下 WPA 加密無法正常運作，只能使用開放網路。

---

### 步驟二：讓 NetworkManager 忽略 wlan1

```bash
sudo nano /etc/NetworkManager/conf.d/99-unmanaged.conf
```

```ini
[keyfile]
unmanaged-devices=interface-name:wlan1
```

```bash
sudo systemctl restart NetworkManager
```

---

### 步驟三：設定 wlan1 靜態 IP

```bash
sudo nano /etc/systemd/network/10-wlan1.network
```

```ini
[Match]
Name=wlan1

[Network]
Address=192.168.4.1/24
```

```bash
sudo systemctl enable systemd-networkd
sudo systemctl restart systemd-networkd
```

---

### 步驟四：設定 hostapd

```bash
sudo nano /etc/hostapd/hostapd.conf
```

```
interface=wlan1
driver=nl80211
ssid=my_rpi_AP
hw_mode=g
channel=6
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
```

```bash
sudo nano /etc/default/hostapd
# 修改為：
DAEMON_CONF="/etc/hostapd/hostapd.conf"

sudo systemctl unmask hostapd
sudo systemctl enable hostapd
sudo systemctl restart hostapd
```

---

### 步驟五：設定 dnsmasq（DHCP）

```bash
sudo mv /etc/dnsmasq.conf /etc/dnsmasq.conf.bak
sudo nano /etc/dnsmasq.conf
```

```
interface=wlan1
bind-interfaces
dhcp-range=192.168.4.10,192.168.4.50,255.255.255.0,24h
domain=local
address=/gw.local/192.168.4.1
```

> `bind-interfaces` 確保 dnsmasq 只監聽 wlan1，不影響 wlan0 的 DNS。不使用 DNS 劫持，避免干擾多網卡 client 的其他連線。

```bash
sudo systemctl enable dnsmasq
sudo systemctl restart dnsmasq
```

---

### 步驟六：Captive Portal

**運作原理**

1. client 連上 `my_rpi_AP` 取得 IP
2. OS 自動發送 HTTP 連線偵測（`/hotspot-detect.html`、`/generate_204` 等）
3. iptables 將所有 port 80 流量導向 Flask（192.168.4.1:8080）
4. Flask 回傳 302，OS 跳出 Captive Portal 視窗
5. 使用者註冊並等待 Admin 在 Dashboard 審核通過
6. 登入後進入 Status 頁面，IP 自動加入 iptables 白名單，可正常上網

**啟動**

```bash
export FLASK_SECRET_KEY="your-random-secret"
export ADMIN_USER="admin"
export ADMIN_PASSWORD_HASH="pbkdf2:sha256:..."

sudo bash portal/setup_iptables.sh
sudo -E python3 portal/app.py
```

> `portal.db` 會在第一次啟動時自動建立於 `portal/` 目錄下。

---

## QoS 設定（configs/qos_policy.yaml）

定義各流量 class 的頻寬分配，修改後重啟 qos_controller 生效：

| Class | rate | ceil | priority |
|-------|------|------|----------|
| VOIP | 10 Mbit | 30 Mbit | 0（最高）|
| STREAMING | 20 Mbit | 60 Mbit | 1 |
| CHAT | 5 Mbit | 20 Mbit | 2 |
| BROWSING | 10 Mbit | 50 Mbit | 3 |
| MAIL | 2 Mbit | 20 Mbit | 5 |
| FT | 5 Mbit | 40 Mbit | 6 |
| P2P | 2 Mbit | 10 Mbit | 7（最低）|

---

## 模型部署（models/）

支援三種模型：**RandomForest**（rf）、**XGBoost**（xgb）、**LightGBM**（lgb）。

- RF：skl2onnx 匯出，直接輸出字串 label，無需查表
- XGB / LGB：輸出 int index，啟動時自動載入 `models/label_classes.txt` 查表；兩者共用同一份

查詢訓練端 releases 有哪些模型可用：

```bash
curl -s https://api.github.com/repos/hyouka0904/encrypted-traffic-train/releases | python3 -c "
import sys, json
releases = json.load(sys.stdin)
for r in releases:
    print(r['tag_name'], [a['name'] for a in r['assets']])
"
```

下載模型：

```bash
mkdir -p models

# RandomForest
wget https://github.com/hyouka0904/encrypted-traffic-train/releases/download/v1.1-rf/rf.onnx -O models/rf.onnx
wget https://github.com/hyouka0904/encrypted-traffic-train/releases/download/v1.1-rf/features.txt -O models/features.txt

# XGBoost（含 label_classes.txt，lgb 共用）
wget https://github.com/hyouka0904/encrypted-traffic-train/releases/download/v1.1-xgb/xgb.onnx -O models/xgb.onnx
wget https://github.com/hyouka0904/encrypted-traffic-train/releases/download/v1.1-xgb/label_classes.txt -O models/label_classes.txt

# LightGBM
wget https://github.com/hyouka0904/encrypted-traffic-train/releases/download/v1.1-lgb/lgb.onnx -O models/lgb.onnx
```

> `latest` tag 不可靠（會指向最新 release），請務必指定 tag。

---

## flow_monitor 設計說明

### 特徵

`features.txt` 定義 15 個特徵的名稱與順序，`flow_monitor` 啟動時讀取，不在程式碼中硬編碼。目前使用的 15 個特徵：

```
duration, flowPktsPerSecond, flowBytesPerSecond,
min_flowiat, max_flowiat, mean_flowiat, std_flowiat,
min_active, mean_active, max_active, std_active,
min_idle, mean_idle, max_idle, std_idle
```

排除的 8 個 fiat/biat 欄位在 ISCX ARFF 中數值與語意不符（min > max、數量級錯誤），scapy 無法正確重現，詳見訓練端 README。

### 時間單位

duration、flowiat、active、idle 統一使用**微秒（μs）**，與訓練資料集一致。`flowPktsPerSecond` / `flowBytesPerSecond` 維持 per-second。

### Sliding window

每條 flow 保留最近 **15 秒**的封包時間戳（`all_times`），每 **5 秒**做一次推論（tick）。推論完不清空，只丟棄 window 外的舊時間戳。

**近似限制**：`fwd/bwd_bytes`、`active_periods`、`idle_periods` 無對應時間戳，不隨 window 裁切，rate 與 active/idle 統計為整條 flow 歷史的近似值，非嚴格 15s window。

### active / idle

封包間隔超過 **5,000,000 μs（5s）** 視為 idle，觸發 active period 結算。無 active 或 idle 資料時，對應特徵填 **-1**（對齊 ISCX 資料集）。推論時不呼叫 `close()`，改為直接計算當前 active 段。

### 多數決

同一個 client IP 可能同時有多條 flow（src/dst 方向不同），每個 tick 對同一 IP 的所有 flow 各自推論後，取票數最多的 label 作為該 IP 的結果。

### 穩定機制

每個 IP 維護最近 3 個 tick 的推論結果，連續 3 次相同才真正呼叫 `qos_controller` 改 mark，避免頻繁切換限速規則。

### 推論 log

每個 stable tick append 一筆到 `logs/flow_infer_log.csv`，格式 `timestamp,ip,label,model`。此檔持續累積、不推 git，作為 Portal 截取實驗區間的原始資料來源。`model` 欄位記錄當前 flow_monitor 載入的模型（rf / xgb / lgb），因為模型在 flow_monitor 啟動時就固定、無法中途更換，Portal 截取時直接從此欄得知是哪個模型。

### 效能量測

每 10 個 tick 印一次統計摘要，包含推論耗時（mean / max / std，單位 ms）、CPU / RAM、label 分布。

### 指定模型

`--model` 參數指定模型名稱（不含 `.onnx`），找不到時列出 `models/` 內可用清單。

```bash
sudo venv/bin/python inference/flow_monitor.py --iface wlan1 --model rf
sudo venv/bin/python inference/flow_monitor.py --iface wlan1 --model xgb
sudo venv/bin/python inference/flow_monitor.py --iface wlan1 --model lgb
```

---

## qos_controller 實作說明

### iptables chain 選擇

fwmark 需在 `mangle POSTROUTING` chain 打，而非 `FORWARD`。原因：tc egress 在封包即將離開介面時才套用，`FORWARD` 打的 mark 在某些情況下會被 conntrack 在後續處理中清掉，導致 tc fw filter 無法讀到。`POSTROUTING` 是封包離開前的最後一個 hook，確保 mark 保留到 tc 讀取。

### clear_all 設計

`_marked_ips` 字典存在記憶體中，重新執行 Python 即清空，無法依賴它還原 iptables 規則。`clear_all()` 改為直接 `iptables -t mangle -F POSTROUTING`，不依賴記憶體狀態。同理，`_mark_ip()` 在新增規則前先查詢 iptables 刪除該 IP 的舊規則，避免重複呼叫累積殘留規則。

---

## QoS 單機測試與 iperf3 驗證

不跑完整 flow_monitor，直接測試特定 IP 的限速效果：

```bash
# 套用限速
sudo venv/bin/python inference/qos_controller.py --ip 192.168.4.2 --label P2P

# 驗證規則
sudo tc -s class show dev wlan1
sudo iptables -t mangle -L -n -v

# 清除
sudo venv/bin/python inference/qos_controller.py --clear
```

**iperf3 限速效果驗證**：Pi 端開 `iperf3 -s`，client 端使用 `-R`（reverse）模式測下行：

```bash
# client_ip 為 client 連上 AP 後拿到的 192.168.4.x IP
# 用 ip addr show 查詢該介面 IP
iperf3 -c 192.168.4.1 -t 15 -R --bind <client_ip>
```

`-R` 讓 Pi 傳資料給 client，才是 tc egress 管到的下行方向。`--bind` 綁定 AP 網段 IP 確保流量走正確介面。

> 驗證紀錄：P2P（ceil=10Mbit）實測壓在約 11.5Mbit，限速生效。STREAMING（ceil=60Mbit）因 WiFi 實際吞吐跑不到 60Mbit，ceil 無從發揮，屬正常現象，驗證時請用低 ceil 的 class（如 P2P）才看得出效果。

Demo 情境：兩台 client 各套不同 class（例如一台 FT、一台 P2P），觀察吞吐量差異。

---

## 流量分類實驗

### 版本控制與目錄

`logs/` 整個目錄不推 git（避免 demo 時多人重複按產生大量 log）；`results/` 推 git（eval 輸出的結果）。`.gitignore` 設定：

```
logs/
!logs/.gitkeep
portal/static/testfile.bin
```

`!logs/.gitkeep` 例外讓空的 `logs/` 目錄能進 git，否則 clone 下來不會有此目錄。即便如此，`flow_monitor.py` 和 `app.py` 在寫檔前都應先 `mkdir -p logs`，確保目錄存在不會出錯。`results/` 同理保留 `.gitkeep`。

### 設計概念

實驗目的是量測三個模型（rf / xgb / lgb）在即時部署環境下對各流量類別的分類準確度。為了控制變因，流量由 **status 頁面的 JavaScript 腳本**按固定序列自動產生，而非手動操作或外部工具。整個實驗在使用者登入 Portal 後、於瀏覽器內完成。

### 為什麼用瀏覽器 JavaScript 產生流量

- 流量必須在登入認證後才能送出（未登入被 iptables FORWARD DROP 擋下），由 status 頁面發起天然滿足此前提
- 不需在 client 端額外安裝或執行任何工具（iperf3 / 腳本），降低操作複雜度與跨機器同步問題
- 由腳本控制序列與時間，控制變因，三個模型跑同一份序列，各自評分（如同不同模型寫同一份考卷）

### 流量產生方式

status 頁面的「Start Experiment」按鈕按下後，JavaScript 依 `ground_truth.csv` 的固定序列自動產生流量，跑完自動結束。各類別的產生方式：

- **BROWSING**：反覆對 Portal 發短 HTTP GET（間隔數秒），模擬瀏覽行為
- **FT**：持續 fetch 大檔案 `/static/testfile.bin`（由 app.py 提供，見下），模擬檔案傳輸的持續大流量
- **P2P**：低頻寬、分散的 fetch，模擬 P2P 的小量持續流量

### 測試大檔案（FT 用）

FT 流量需要一個大檔案 endpoint。在 Pi 上產生一個假的大檔案（內容不重要，純粹製造流量）：

```bash
# 產生 100MB 的測試檔
mkdir -p portal/static
head -c 100M /dev/urandom > portal/static/testfile.bin
```

app.py 透過 Flask static 路由提供此檔。`testfile.bin` 已加入 `.gitignore`，不推 git（檔案大且無保留價值，每台機器自行產生即可）。

### 未納入的類別

VOIP / STREAMING / CHAT 未納入實驗：

- **VOIP**：需 RTP/UDP 封包特徵與特定封包間隔，瀏覽器 fetch 無法產生
- **STREAMING**：需持續大流量加特定 IAT 模式，與單純 fetch 大檔案的特徵不同
- **CHAT**：需短封包、低頻率的雙向互動，與一般 HTTP GET 的特徵差異微妙，難以忠實模擬

這三類列為 future work，根本解是取得能忠實產生對應流量的工具或真實流量重放。

### 實驗流程

1. **Pi**：啟動 Portal（`portal/app.py`）與 flow_monitor（指定 model），flow_monitor 持續寫 `logs/flow_infer_log.csv`
2. **ThinkPad**：連上 `my_rpi_AP`，通過 Captive Portal 登入，進入 status 頁面
3. **ThinkPad**：按「Start Experiment」。瀏覽器打 `POST /experiment/start` 通知 Portal 記下開始時間與 client IP，接著 JavaScript 依固定序列自動產生流量
4. 序列跑完，JavaScript 自動打 `POST /experiment/stop`。Portal 從 `flow_infer_log.csv` 截取「開始～結束時間內、該 client IP」的推論紀錄，換算成相對 offset（從實驗開始算起的秒數），存成 `logs/infer_<model>_<timestamp>.csv`（不推 git，避免 demo 時多人重複按造成大量 log）
5. 三個模型各跑一次完整流程，各自產生一份 infer log

### ground truth（experiment/ground_truth.csv）

預先產生的固定序列，使用相對時間偏移，格式 `offset_sec,label`：

```
offset_sec,label
0,BROWSING
45,FT
115,P2P
170,FT
205,BROWSING
285,P2P
325,BROWSING
385,FT
435,P2P
510,END
```

每段流量在 offset_sec 開始，下一段的 offset_sec 為前一段結束。`END` 標記實驗總長。因為使用相對偏移而非絕對時間戳，序列可以預先固定並推 git，不需每次實驗重新記錄。同種流量不連續出現、時間長短不規則，避免模型靠順序規律預測。

### 準確度評估（experiment/eval.py）

實驗結束後，將 infer log 與 ground_truth.csv 對照計算準確度：

```bash
python experiment/eval.py --infer logs/infer_rf_<timestamp>.csv
```

- `--infer`：指定要評估的 infer log
- `--gt`：預設讀 `experiment/ground_truth.csv`，不需指定
- 輸出：自動產生 `results/result_<model>_<timestamp>.json`（model 與 timestamp 從 infer log 檔名解析），內含準確度統計、逐筆明細、ground truth 序列三部分

三個模型各跑一次、各自指定其 infer log，產生三份 result。

**對齊邏輯**：ground_truth 定義時間區間（offset 0\~45 為 BROWSING，45\~115 為 FT，依此類推）。infer log 每筆推論的 offset 落在哪個區間，就以該區間的 label 為 ground truth 比對。推論筆數不需與 ground truth 段數相同，每筆獨立判斷。

**準確度計算**：
- 整體準確度 = 所有推論中 predicted 等於所在區間 ground truth 的比例
- per-label 準確度 = 只看落在該 label 區間內的推論，算對的比例
- per-label 另記錄 `predicted_as`（該區間內被預測成哪些 label 各幾次），用來看模型把某類別錯認成什麼，定位錯誤模式

---

## 啟動流程

系統服務（hostapd、dnsmasq）開機自啟，手動啟動的服務依序如下：

**1. 初始化 iptables 攔截規則**
```bash
sudo bash portal/setup_iptables.sh
```

**2. 啟動 Captive Portal**
```bash
source venv/bin/activate
export FLASK_SECRET_KEY="..." ADMIN_USER="admin" ADMIN_PASSWORD_HASH="..."
sudo -E venv/bin/python portal/app.py
```

**3. 啟動流量監控與 QoS（需先確認 models/ 已放好 ONNX）**
```bash
source venv/bin/activate
sudo venv/bin/python inference/flow_monitor.py --iface wlan1 --model rf
```

> `flow_monitor` 啟動時會自動 import `qos_controller`，後者在 import 時執行 `init_tc()` 初始化 tc HTB。不需要單獨啟動 `qos_controller`。

---

## 關閉流程

```bash
# 停止 flow_monitor：Ctrl+C 或 kill
# 清除 tc / iptables QoS 規則
sudo venv/bin/python inference/qos_controller.py --clear

# 停止 Portal：Ctrl+C 或 kill
```

> 重開機後 tc 與 iptables 規則自動消失，不需要手動清除。

---

## 連線方式

連上 `my_rpi_AP`（無密碼），通過 Captive Portal 後可正常上網，亦可 SSH：

```bash
ssh user@192.168.4.1
```

DHCP 派發範圍：`192.168.4.10` ~ `192.168.4.50`

---

## 待辦

### 【最高優先】流量分類實驗

- [ ] RF / XGB / LGB 三模型各跑一次完整實驗流程，各自產生 infer log，用 eval.py 計算準確度
- [ ] **修 eval.py 輸出**：目前 eval.py 對齊與計算邏輯已完成，但輸出格式需改為單一 result 檔案，存到 `results/result_<model>_<timestamp>.json`（model 與 timestamp 從 infer log 檔名解析）。檔案內含三部分：(1) 準確度統計（overall + per-label + predicted_as）、(2) 逐筆推論明細（offset, ip, predicted, ground_truth, correct）、(3) 使用的 ground truth 序列本身。`results/` 推 git，`logs/` 全部不推
- [ ] 報告分開陳述「離線分類效能（test set 數字）」與「部署整合（即時迴路）」
- [ ] 即時特徵保真度列為 future work：fiat/biat 欄位定義與命名不一致，即時 scapy 無法忠實重現；根本解是取得 ISCX 原始 PCAP、用自己控制的特徵提取重算 + 重訓（見訓練端 README）
- [ ] 流量類別 future work：VOIP / STREAMING / CHAT 無法用瀏覽器 fetch 忠實模擬，需找對應工具或真實流量重放

---

### Portal / 登入頁面
- [ ] 每次斷開連接都要重登：portal 認證狀態記憶體存放，重啟或斷線即消失，需持久化（iptables 白名單 + session 同步寫回磁碟）
- [ ] systemd 開機自啟：portal 服務開機自動啟動
- [ ] 登入頁加入使用條款（Agreement notice）與專案說明
- [ ] Status 頁面補充內容：DHCP 租約列表（從 dnsmasq leases 讀取）等

### Admin Dashboard
- [ ] 即時監控儀表板：連線裝置數、即時頻寬、延遲、CPU/RAM、訊號強度、各裝置流量
- [ ] 用戶行為分析：連線時間、在線時長、每裝置流量、尖峰時段
- [ ] 使用報告：總流量、平均延遲、最活躍裝置、尖峰時段、uptime
- [ ] Web 控制面板：SSID 設定、頻寬限制、裝置封鎖、監控圖表、效能測試

### 資安
- [ ] 未知裝置告警、登入失敗偵測
- [ ] MAC 過濾、黑白名單
- [ ] 異常流量警示

### 可靠性
- [ ] 自動自我修復（Watchdog）：自動重啟 hostapd、重新連網、送出警示記錄
- [ ] 多模式 AP：一般、訪客、低延遲、省電、實驗模式