# encrypted-traffic-deploy

Raspberry Pi 5 AP 部署專案。以 Edimax USB 網卡（wlan1）開 WiFi AP，對連入裝置實施 Captive Portal 強制登入，並以 ONNX Runtime 在 Pi 上執行加密流量分類模型，依推論結果透過 tc HTB 套用 QoS 限速。

**配套訓練專案**：[encrypted-traffic-train](https://github.com/hyouka0904/encrypted-traffic-train)（ISCX-VPN 2016 Scenario B，RandomForest）

---

## 系統架構

```
client 連上 my_rpi_AP
        │
        ▼
Captive Portal（Flask, portal/app.py）
  └─ 未登入 → 攔截導向登入頁
  └─ 登入後 → iptables 白名單放行上網
        │
        ▼
flow_monitor（inference/flow_monitor.py）
  └─ scapy 即時抓 wlan1 封包 → 抽 15 特徵 → ONNX Runtime 推論
        │
        ▼
qos_controller（inference/qos_controller.py）
  └─ iptables mangle 打 fwmark → tc HTB 依 class 限速（下行，wlan1 egress）
```

---

## 目錄結構

```
encrypted-traffic-deploy/
├── portal/
│   ├── app.py                 # Flask Captive Portal
│   ├── portal.db              # SQLite（自動建立）
│   ├── setup_iptables.sh      # iptables 攔截規則初始化
│   └── templates/
│       ├── portal.html
│       ├── login.html
│       ├── register.html
│       ├── register_success.html
│       ├── status.html
│       ├── admin_login.html
│       └── admin_dashboard.html
├── inference/
│   ├── flow_monitor.py        # 即時封包抓取與特徵萃取
│   └── qos_controller.py      # tc HTB 初始化與 iptables fwmark 控制
├── models/
│   └── features.txt           # 特徵欄位順序（與訓練端一致，15 欄）
├── configs/
│   └── qos_policy.yaml        # 各流量 class 的頻寬設定
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

部署使用 **RandomForest** 模型（skl2onnx 匯出，輸出字串 label，無需查表）。

查詢訓練端 releases 有哪些模型可用：

```bash
curl -s https://api.github.com/repos/hyouka0904/encrypted-traffic-train/releases | python3 -c "
import sys, json
releases = json.load(sys.stdin)
for r in releases:
    print(r['tag_name'], [a['name'] for a in r['assets']])
"
```

下載指定版本（以 `v1.1-rf` 為例）：

```bash
mkdir -p models
wget https://github.com/hyouka0904/encrypted-traffic-train/releases/download/v1.1-rf/rf.onnx -O models/rf.onnx
wget https://github.com/hyouka0904/encrypted-traffic-train/releases/download/v1.1-rf/features.txt -O models/features.txt
```

> `latest` tag 不可靠（會指向最新 release，不一定含 rf.onnx），請務必指定 tag。

> xgb / lgb 輸出 int index，需搭配 `label_classes.txt` 查表，整合較複雜。部署固定用 RandomForest。

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

### 指定模型

`--model` 參數指定模型名稱（不含 `.onnx`），找不到時列出 `models/` 內可用清單：

```bash
# 預設 rf
sudo venv/bin/python inference/flow_monitor.py --iface wlan1

# 指定模型
sudo venv/bin/python inference/flow_monitor.py --iface wlan1 --model xgb

# 找不到時提示可用清單
sudo venv/bin/python inference/flow_monitor.py --iface wlan1 --model abc
```

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

## QoS 單機測試與 iperf3 驗證

不跑完整 flow_monitor，直接測試特定 IP 的限速效果：

```bash
# 套用限速
sudo venv/bin/python inference/qos_controller.py --ip 192.168.4.2 --label STREAMING

# 驗證規則
sudo tc -s class show dev wlan1
sudo iptables -t mangle -L -n -v

# 清除
sudo venv/bin/python inference/qos_controller.py --clear
```

**iperf3 限速效果驗證**：Pi 端開 `iperf3 -s`，client 端 `iperf3 -c 192.168.4.1 -t 10`。
Demo 情境：兩台 client 各套不同 class（例如一台 STREAMING、一台 P2P），觀察吞吐量差異。
**每台 client 對應一種 app 類型**，迴避 per-flow 分類 vs per-IP 限速的粒度衝突。

---

## 連線方式

連上 `my_rpi_AP`（無密碼），通過 Captive Portal 後可正常上網，亦可 SSH：

```bash
ssh user@192.168.4.1
```

DHCP 派發範圍：`192.168.4.10` ~ `192.168.4.50`

---

## 待辦

### 【最高優先】ML 推論整合

**保真度驗證（先驗證再相信）**
- [ ] 用 `flow_monitor` 對已知流量算出特徵，跟 `train.csv` 同類別的分布比對
- [ ] `flowiat` / 兩個 rate / `duration` 應該對得上
- [ ] `active` / `idle` 若對不上，退回訓練端把 active/idle 也一起移除重訓

**`qos_controller.py` 正確性確認**
- [ ] 確認 fwmark 方向：要整形**下行**（client 看影片/下載），mark 必須打在「目的地是 client」的封包（`-d <client_ip>`），且 tc HTB 掛在 wlan1 egress。**若目前是 `-s <client_ip>`（上行，走 wlan0），下行限速不會生效，需修正**

**Demo 主線：即時分類迴路**
- [ ] client 端跑已知 app（事先排好順序，例如 VOIP 30s → STREAMING 30s）
- [ ] `flow_monitor` 在 Pi 即時抓封包 → 推論 → 印 log，現場對照「跑的是什麼 vs 模型猜什麼」
- [ ] 加最小 ground-truth 記錄機制（標記現在跑什麼 app）供 log 對照
- [ ] QoS 限速效果用 iperf3 驗證（Pi 開 `iperf3 -s`，兩台 client 各跑一種 app，看吞吐量差異）

**報告**
- [ ] 報告分開陳述「離線分類效能（test set 數字）」與「部署整合（即時迴路，本次新打通）」
- [ ] 即時特徵保真度列為 future work：fiat/biat 欄位定義與命名不一致，即時 scapy 無法忠實重現；根本解是取得 ISCX 原始 PCAP、用自己控制的特徵提取重算 + 重訓（見訓練端 README）

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