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
pip3 install -r requirements.txt --break-system-packages
```

Portal 使用 `sudo` 執行，需另外為 root 安裝 Flask：

```bash
sudo python3 -m pip install flask --break-system-packages
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

採用純 iptables 攔截方式，不做 DNS 劫持，不影響 client 其他網卡的連線。

**運作原理**

1. client 連上 `my_rpi_AP` 取得 IP
2. OS 自動發送 HTTP 連線偵測（`/hotspot-detect.html`、`/generate_204` 等）
3. iptables 將所有 port 80 流量導向 Flask（192.168.4.1:8080）
4. Flask 回傳 302，OS 跳出 Captive Portal 視窗
5. 使用者點擊「連線」→ Flask 將該 IP 加入 iptables 白名單 → 放行所有流量

**目錄結構**

```
portal/
├── app.py
├── setup_iptables.sh
└── templates/
    └── portal.html
```

**啟動**

```bash
sudo bash portal/setup_iptables.sh
sudo python3 portal/app.py
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

- [ ] 每次斷開連接都要重登
- [ ] **systemd 開機自啟**：portal 服務開機自動啟動
- [ ] **模型載點**：在 [encrypted-traffic-train](https://github.com/hyouka0904/encrypted-traffic-train) 發布 GitHub Releases，並在此加入下載指令將模型放至 `models/`
- [ ] **流量請求網站**：讓 client 可選擇不同流量類型（影片串流、檔案下載、網頁瀏覽等），供 QoS 實驗使用
  - ⚠️ 此實驗**必須在 wlan0 正常轉送的前提下進行**。若僅提供本機服務，流量特徵與 ISCX-VPN 訓練資料不符，模型分類結果無參考價值（僅可做 pipeline 功能測試）
- [ ] ML 模型整合（`models/`）：接上 flow_monitor → policy → controller 完整流程
- [ ] QoS policy / monitor / controller 設計