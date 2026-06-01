# encrypted-traffic-deploy

Raspberry Pi 5 AP 部署專案。以 Edimax USB 網卡（wlan1）開 WiFi AP，對連入裝置實施 Captive Portal 強制登入，並以 ONNX Runtime 在 Pi 上執行加密流量分類模型，依推論結果透過 tc HTB 套用 QoS 限速。

**配套訓練專案**：[encrypted-traffic-train](https://github.com/hyouka0904/encrypted-traffic-train)（ISCX-VPN 2016 Scenario B，RandomForest，macro F1 0.886）

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
  └─ scapy 抓 wlan1 封包 → 每 5 秒抽特徵 → ONNX Runtime 推論
        │
        ▼
qos_controller（inference/qos_controller.py）
  └─ iptables mangle FORWARD 打 fwmark → tc HTB 依 class 限速
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
│   ├── model.onnx             # 訓練端匯出的 RandomForest ONNX 模型
│   └── features.txt           # 特徵欄位順序（與訓練端一致）
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

從訓練端的 GitHub Releases 下載 `model.onnx` 與 `features.txt`，放入 `models/`：

```bash
mkdir -p models
# 從 encrypted-traffic-train releases 下載
wget -O models/model.onnx  <release_url>/model.onnx
wget -O models/features.txt <release_url>/features.txt
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
sudo venv/bin/python inference/flow_monitor.py --iface wlan1
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

## QoS 單機測試

不跑完整 flow_monitor，直接測試特定 IP 的限速效果：

```bash
# 套用限速
sudo venv/bin/python inference/qos_controller.py --ip 192.168.4.2 --label STREAMING

# 驗證規則
sudo tc -s class show dev wlan1
sudo iptables -t mangle -L FORWARD -n -v

# 清除
sudo venv/bin/python inference/qos_controller.py --clear
```

---

## 連線方式

連上 `my_rpi_AP`（無密碼），通過 Captive Portal 後可正常上網，亦可 SSH：

```bash
ssh user@192.168.4.1
```

DHCP 派發範圍：`192.168.4.10` ~ `192.168.4.50`

---

## 待完成

**ML 推論整合**
- [ ] 從 encrypted-traffic-train releases 下載 model.onnx 到 Pi 的 models/
- [ ] replay 腳本（inference/replay.py）：讀 test.csv 隨機幾列 → ONNX Runtime 推論 → 印預測 vs 真值 → 呼叫 qos_controller 套 QoS
- [ ] flow_monitor.py 修正：時間單位改為微秒（×1e6）、修 extract_features 重複呼叫 flow.close() 導致 active_periods 累加的 bug、改從 features.txt 讀取特徵順序

**Portal / 登入頁面**
- [ ] 每次斷開連接都要重登：portal 認證狀態記憶體存放，重啟或斷線即消失，需持久化（iptables 白名單 + session 同步寫回磁碟）
- [ ] systemd 開機自啟：portal 服務開機自動啟動
- [ ] 登入頁加入使用條款（Agreement notice）與專案說明
- [ ] Status 頁面補充內容：DHCP 租約列表（從 dnsmasq leases 讀取）等

**Admin Dashboard**
- [ ] 即時監控儀表板：連線裝置數、即時頻寬、延遲、CPU/RAM、訊號強度、各裝置流量
- [ ] 用戶行為分析：連線時間、在線時長、每裝置流量、尖峰時段
- [ ] 使用報告：總流量、平均延遲、最活躍裝置、尖峰時段、uptime
- [ ] Web 控制面板：SSID 設定、頻寬限制、裝置封鎖、監控圖表、效能測試

**資安**
- [ ] 未知裝置告警、登入失敗偵測
- [ ] MAC 過濾、黑白名單
- [ ] 異常流量警示

**可靠性**
- [ ] 自動自我修復（Watchdog）：自動重啟 hostapd、重新連網、送出警示記錄
- [ ] 多模式 AP：一般、訪客、低延遲、省電、實驗模式