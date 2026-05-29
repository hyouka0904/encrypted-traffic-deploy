# encrypted-traffic-deploy

Raspberry Pi 5 AP 部署專案，提供 Captive Portal 登入、流量分類與 QoS 控制。

**配套訓練專案**：[encrypted-traffic-train](https://github.com/hyouka0904/encrypted-traffic-train)

---

## Clone

```bash
git clone https://github.com/hyouka0904/encrypted-traffic-deploy.git
cd encrypted-traffic-deploy
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

將輸出的字串設為 `ADMIN_PASSWORD_HASH`。

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

採用純 iptables 攔截方式，不做 DNS 劫持，不影響 client 其他網卡的連線。

**運作原理**

1. client 連上 `my_rpi_AP` 取得 IP
2. OS 自動發送 HTTP 連線偵測（`/hotspot-detect.html`、`/generate_204` 等）
3. iptables 將所有 port 80 流量導向 Flask（192.168.4.1:8080）
4. Flask 回傳 302，OS 跳出 Captive Portal 視窗
5. 使用者註冊並等待 Admin 在 Dashboard 審核通過
6. 登入後進入 Status 頁面，IP 自動加入 iptables 白名單，可正常上網

**目錄結構**

```
portal/
├── app.py
├── portal.db              # SQLite（自動建立）
├── setup_iptables.sh
└── templates/
    ├── portal.html            # 首頁（登入/註冊入口）
    ├── login.html             # 使用者登入
    ├── register.html          # 帳號申請
    ├── register_success.html  # 申請成功（等待審核提示）
    ├── status.html            # 登入後狀態頁
    ├── admin_login.html       # Admin 登入
    └── admin_dashboard.html   # Admin 審核 + 已授權 IP 列表
```

**啟動**

```bash
export FLASK_SECRET_KEY="your-random-secret"
export ADMIN_USER="admin"
export ADMIN_PASSWORD_HASH="pbkdf2:sha256:..."   # 用上方指令產生

sudo bash portal/setup_iptables.sh
sudo -E python3 portal/app.py
```

> `portal.db` 會在第一次啟動時自動建立於 `portal/` 目錄下。

---

## 連線方式

連上 `my_rpi_AP`（無密碼），通過 Captive Portal 後可正常上網，亦可 SSH：

```bash
ssh user@192.168.4.1
```

DHCP 派發範圍：`192.168.4.10` ~ `192.168.4.50`

---

## 待完成

**Portal / 登入頁面**
- [ ] **每次斷開連接都要重登**：portal 認證狀態記憶體存放，重啟或斷線即消失，需持久化（iptables 白名單 + session 同步寫回磁碟）
- [ ] systemd 開機自啟：portal 服務開機自動啟動
- [ ] 登入頁加入使用條款（Agreement notice）與專案說明
- [ ] Status 頁面補充內容（目前預留空白）：DHCP 租約列表（從 dnsmasq leases 讀取）等

**Admin Dashboard**
- [ ] 即時監控儀表板：連線裝置數、即時頻寬、延遲、CPU/RAM、訊號強度、各裝置流量
- [ ] 用戶行為分析：連線時間、在線時長、每裝置流量、尖峰時段
- [ ] 使用報告：總流量、平均延遲、最活躍裝置、尖峰時段、uptime
- [ ] Web 控制面板：SSID 設定、頻寬限制、裝置封鎖、監控圖表、效能測試

**流量管理**
- [ ] 個別頻寬控制（Per-user Bandwidth Control）：使用 `tc` 實作，可依群組設定不同速度
- [ ] QoS 優先權模式：視訊會議（高）、遊戲（低延遲）、網頁（一般）、下載（低優先）
- [ ] 流量實驗 API：新增 `POST /experiment/start {"ip": "...", "label": "VOIP"}` 與 `POST /experiment/stop` endpoint，供 client 腳本標記 ground truth；Pi 端紀錄時間段與 ML 推論結果，可在 Admin Dashboard 顯示對照精確度

**ML 推論整合**
- [ ] ML 模型整合（`models/`）：接上 flow_monitor → policy → controller 完整流程
- [ ] QoS policy / monitor / controller 設計

**資安**
- [ ] 未知裝置告警、登入失敗偵測
- [ ] MAC 過濾、黑白名單
- [ ] 異常流量警示

**可靠性**
- [ ] 自動自我修復（Watchdog）：自動重啟 hostapd、重新連網、送出警示記錄
- [ ] 多模式 AP：一般、訪客、低延遲、省電、實驗模式