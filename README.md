# Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Usage

**啟動流量監控與 QoS 控制**
```bash
sudo python inference/flow_monitor.py
```
# Raspberry Pi AP 設定說明

**裝置**：Raspberry Pi 5 (8GB)，hostname: `MadChicken`
**目標**：讓 wlan1（USB 網卡）作為 AP，在沒有外部網路時仍可透過 WiFi 連上樹莓派

---

## 網路介面配置

| 介面 | 硬體 | 角色 |
|------|------|------|
| `wlan0` | 內建 WiFi | 連接家裡 WiFi（上網用） |
| `wlan1` | Edimax N150（RTL8188EU, `7392:b811`） | AP（`my_rpi_AP`） |

---

## 步驟一：更換 wlan1 驅動

預設的 `rtl8xxxu` 驅動不支援 AP 模式，需換成 `rtl8188eus`。

```bash
# 安裝編譯依賴
sudo apt install -y git build-essential dkms bc linux-headers-$(uname -r)

# 下載驅動原始碼
git clone https://github.com/aircrack-ng/rtl8188eus.git
cd rtl8188eus

# Blacklist 舊驅動
echo "blacklist rtl8xxxu" | sudo tee /etc/modprobe.d/rtl8188eus.conf

# 編譯並安裝
sudo make
sudo make install

# 載入新驅動
sudo modprobe -r rtl8xxxu
sudo modprobe 8188eu
```

> **注意**：使用 DKMS 概念安裝，kernel 小版本升級會自動重編。大版本升級（約每 2 年）時若編譯失敗，`wlan1` 會暫時消失，但 `wlan0` 不受影響。

---

## 步驟二：讓 NetworkManager 忽略 wlan1

```bash
sudo nano /etc/NetworkManager/conf.d/99-unmanaged.conf
```

填入：

```ini
[keyfile]
unmanaged-devices=interface-name:wlan1
```

```bash
sudo systemctl restart NetworkManager
```

---

## 步驟三：設定 wlan1 靜態 IP

```bash
sudo nano /etc/systemd/network/10-wlan1.network
```

填入：

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

## 步驟四：設定 hostapd

```bash
sudo nano /etc/hostapd/hostapd.conf
```

填入：

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

> **重要**：RTL8188EU 驅動在 AP 模式下 WPA 加密無法正常運作，只能使用開放網路。

設定 hostapd 設定檔路徑：

```bash
sudo nano /etc/default/hostapd
```

修改為：

```
DAEMON_CONF="/etc/hostapd/hostapd.conf"
```

啟用服務：

```bash
sudo systemctl unmask hostapd
sudo systemctl enable hostapd
sudo systemctl restart hostapd
```

---

## 步驟五：設定 dnsmasq（DHCP）

備份原設定：

```bash
sudo mv /etc/dnsmasq.conf /etc/dnsmasq.conf.bak
```

建立新設定：

```bash
sudo nano /etc/dnsmasq.conf
```

填入：

```
interface=wlan1
dhcp-range=192.168.4.10,192.168.4.50,255.255.255.0,24h
domain=local
address=/gw.local/192.168.4.1
```

```bash
sudo systemctl enable dnsmasq
sudo systemctl restart dnsmasq
```

---

## 連線方式

連上 `my_rpi_AP`（無密碼），即可 SSH 進入：

```bash
ssh user@192.168.4.1
```

DHCP 派發範圍：`192.168.4.10` ~ `192.168.4.50`

---

## 待完成

- [ ] Captive Portal（強制登入頁面）：連上 AP 後自動跳出驗證網頁
  - iptables 導流 HTTP/HTTPS 至本機
  - nginx 或 Python Flask 登入頁面
  - 驗證後放行流量