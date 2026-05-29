import os
import subprocess
from functools import wraps

from flask import Flask, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")

PORTAL_IP = "192.168.4.1"
PORTAL_PORT = 8080
AP_IFACE = "wlan1"

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")

authorized_ips = set()


def client_ip():
    return request.remote_addr


def is_authorized(ip):
    return ip in authorized_ips


def authorize_ip(ip):
    if ip in authorized_ips:
        return
    authorized_ips.add(ip)
    subprocess.run(
        ["iptables", "-I", "FORWARD", "-s", ip, "-i", AP_IFACE, "-j", "ACCEPT"],
        check=False,
    )
    subprocess.run(
        [
            "iptables", "-t", "nat", "-I", "PREROUTING",
            "-s", ip, "-i", AP_IFACE, "-p", "tcp", "--dport", "80", "-j", "ACCEPT",
        ],
        check=False,
    )
    print(f"[+] Authorized: {ip}")


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


# ── Captive portal detection ─────────────────────────────────────

@app.route("/generate_204")
def android_check():
    if is_authorized(client_ip()):
        return "", 204
    return redirect(f"http://{PORTAL_IP}:{PORTAL_PORT}/")


@app.route("/hotspot-detect.html")
@app.route("/library/test/success.html")
def apple_check():
    if is_authorized(client_ip()):
        return "<HTML><BODY>Success</BODY></HTML>", 200
    return redirect(f"http://{PORTAL_IP}:{PORTAL_PORT}/")


@app.route("/connecttest.txt")
@app.route("/ncsi.txt")
def windows_check():
    if is_authorized(client_ip()):
        return "Microsoft Connect Test", 200
    return redirect(f"http://{PORTAL_IP}:{PORTAL_PORT}/")


# ── User portal ──────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("portal.html")


@app.route("/user/authorize", methods=["POST"])
def user_authorize():
    ip = client_ip()
    authorize_ip(ip)
    session["role"] = "user"
    return redirect(url_for("user_success"))


@app.route("/user/success")
def user_success():
    return render_template("user_success.html", ip=client_ip())


# ── Admin ────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_logged_in"):
        return redirect(url_for("admin_dashboard"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if (
            username == ADMIN_USER
            and ADMIN_PASSWORD_HASH
            and check_password_hash(ADMIN_PASSWORD_HASH, password)
        ):
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))
        error = "Invalid username or password."

    return render_template("admin_login.html", error=error)


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    return render_template(
        "admin_dashboard.html",
        ips=sorted(authorized_ips),
    )


@app.route("/logout")
def logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("index"))


# ── Catch-all ────────────────────────────────────────────────────

@app.route("/<path:path>")
def catch_all(path):
    if is_authorized(client_ip()):
        return "OK", 200
    return redirect(f"http://{PORTAL_IP}:{PORTAL_PORT}/")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORTAL_PORT, debug=False)
