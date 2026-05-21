from flask import Flask, request, redirect, render_template
import subprocess

app = Flask(__name__)

PORTAL_IP = "192.168.4.1"
PORTAL_PORT = 8080
AP_IFACE = "wlan1"

# 已認證的 IP（記憶體存，重啟清空）
authorized_ips = set()


def client_ip():
    return request.remote_addr

def is_authorized(ip):
    return ip in authorized_ips

def authorize_ip(ip):
    authorized_ips.add(ip)
    # 讓這個 IP 的流量可以 forward
    subprocess.run(["iptables", "-I", "FORWARD", "-s", ip, "-i", AP_IFACE, "-j", "ACCEPT"])
    # 讓這個 IP 的 HTTP 不再被導向 portal
    subprocess.run(["iptables", "-t", "nat", "-I", "PREROUTING",
                    "-s", ip, "-i", AP_IFACE, "-p", "tcp", "--dport", "80", "-j", "ACCEPT"])
    print(f"[+] Authorized: {ip}")


# ── OS 自動連線偵測 ──────────────────────────────

@app.route("/generate_204")           # Android
def android_check():
    if is_authorized(client_ip()):
        return "", 204
    return redirect(f"http://{PORTAL_IP}:{PORTAL_PORT}/")

@app.route("/hotspot-detect.html")    # iOS / macOS
@app.route("/library/test/success.html")
def apple_check():
    if is_authorized(client_ip()):
        return "<HTML><BODY>Success</BODY></HTML>", 200
    return redirect(f"http://{PORTAL_IP}:{PORTAL_PORT}/")

@app.route("/connecttest.txt")        # Windows
@app.route("/ncsi.txt")
def windows_check():
    if is_authorized(client_ip()):
        return "Microsoft Connect Test", 200
    return redirect(f"http://{PORTAL_IP}:{PORTAL_PORT}/")


# ── 主頁 & 認證 ──────────────────────────────────

@app.route("/")
def index():
    return render_template("portal.html")

@app.route("/authorize", methods=["POST"])
def authorize():
    ip = client_ip()
    authorize_ip(ip)
    return redirect("http://example.com")   # 認證後跳到哪裡都可以

# 其他所有 HTTP 請求 → portal
@app.route("/<path:path>")
def catch_all(path):
    if is_authorized(client_ip()):
        return "OK", 200
    return redirect(f"http://{PORTAL_IP}:{PORTAL_PORT}/")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORTAL_PORT, debug=False)