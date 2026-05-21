#!/bin/bash
# 在 Flask 啟動前執行一次

AP_IFACE="wlan1"
WAN_IFACE="wlan0"
PORTAL_IP="192.168.4.1"
PORTAL_PORT="8080"

# IP forwarding
echo 1 > /proc/sys/net/ipv4/ip_forward

# 清除舊規則
iptables -F
iptables -t nat -F

# 預設政策
iptables -P INPUT ACCEPT
iptables -P FORWARD DROP      # 預設全擋，認證後才開
iptables -P OUTPUT ACCEPT

# 允許已建立的連線通過（重要：認證後的流量靠這個）
iptables -A FORWARD -m state --state ESTABLISHED,RELATED -j ACCEPT

# 允許 client 查 DNS（解析用，不劫持）
iptables -A FORWARD -i $AP_IFACE -p udp --dport 53 -j ACCEPT
iptables -A FORWARD -i $AP_IFACE -p tcp --dport 53 -j ACCEPT

# NAT：讓認證後的 client 能上網
iptables -t nat -A POSTROUTING -o $WAN_IFACE -j MASQUERADE

# 允許 client 連到 portal（port 8080）
iptables -A INPUT -i $AP_IFACE -p tcp --dport $PORTAL_PORT -j ACCEPT

# 把所有 HTTP 重導到 portal
iptables -t nat -A PREROUTING -i $AP_IFACE -p tcp --dport 80 \
    -j DNAT --to-destination $PORTAL_IP:$PORTAL_PORT

echo "iptables setup done"