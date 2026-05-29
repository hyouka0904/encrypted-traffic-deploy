import os
import sqlite3
import subprocess
from contextlib import contextmanager
from functools import wraps

from flask import Flask, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")

PORTAL_IP   = "192.168.4.1"
PORTAL_PORT = 8080
AP_IFACE    = "wlan1"

ADMIN_USER          = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")

DB_PATH = os.path.join(os.path.dirname(__file__), "portal.db")

authorized_ips: set[str] = set()


# ── Database ──────────────────────────────────────────────────────────

def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    NOT NULL UNIQUE,
                password_hash TEXT    NOT NULL,
                status        TEXT    NOT NULL DEFAULT 'pending',
                traffic_class TEXT,
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Helpers ───────────────────────────────────────────────────────────

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
        ["iptables", "-t", "nat", "-I", "PREROUTING",
         "-s", ip, "-i", AP_IFACE, "-p", "tcp", "--dport", "80", "-j", "ACCEPT"],
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

def user_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_logged_in"):
            return redirect(url_for("portal"))
        return view(*args, **kwargs)
    return wrapped


# ── Captive portal detection ──────────────────────────────────────────

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


# ── User portal ───────────────────────────────────────────────────────

@app.route("/")
def portal():
    if session.get("user_logged_in"):
        return redirect(url_for("status"))
    if session.get("admin_logged_in"):
        return redirect(url_for("admin_dashboard"))
    return render_template("portal.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if not username or not password:
            error = "Username and password are required."
        else:
            try:
                with get_db() as db:
                    db.execute(
                        "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                        (username, generate_password_hash(password))
                    )
                return render_template("register_success.html", username=username)
            except sqlite3.IntegrityError:
                error = "Username already taken."
    return render_template("register.html", error=error)

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        with get_db() as db:
            user = db.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            error = "Invalid username or password."
        elif user["status"] == "pending":
            error = "Your account is pending admin approval."
        elif user["status"] == "rejected":
            error = "Your account has been rejected."
        else:
            session["user_logged_in"] = True
            session["username"] = username
            session["traffic_class"] = user["traffic_class"]
            authorize_ip(client_ip())
            return redirect(url_for("status"))
    return render_template("login.html", error=error)

@app.route("/status")
@user_required
def status():
    return render_template(
        "status.html",
        ip=client_ip(),
        username=session.get("username"),
        traffic_class=session.get("traffic_class") or "—",
        device_count=len(authorized_ips),
    )

@app.route("/user/logout")
def user_logout():
    session.pop("user_logged_in", None)
    session.pop("username", None)
    session.pop("traffic_class", None)
    return redirect(url_for("portal"))


# ── Admin ─────────────────────────────────────────────────────────────

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
            authorize_ip(client_ip())
            return redirect(url_for("admin_dashboard"))
        error = "Invalid username or password."
    return render_template("admin_login.html", error=error)

@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    with get_db() as db:
        pending = db.execute(
            "SELECT id, username, created_at FROM users WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()
        approved = db.execute(
            "SELECT id, username, traffic_class FROM users WHERE status = 'approved' ORDER BY username"
        ).fetchall()
    return render_template(
        "admin_dashboard.html",
        ips=sorted(authorized_ips),
        pending=pending,
        approved=approved,
    )

@app.route("/admin/approve/<int:user_id>", methods=["POST"])
@admin_required
def admin_approve(user_id):
    with get_db() as db:
        db.execute("UPDATE users SET status = 'approved' WHERE id = ?", (user_id,))
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/reject/<int:user_id>", methods=["POST"])
@admin_required
def admin_reject(user_id):
    with get_db() as db:
        db.execute("UPDATE users SET status = 'rejected' WHERE id = ?", (user_id,))
    return redirect(url_for("admin_dashboard"))

@app.route("/logout")
def logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("portal"))


# ── Catch-all ─────────────────────────────────────────────────────────

@app.route("/<path:path>")
def catch_all(path):
    if is_authorized(client_ip()):
        return "OK", 200
    return redirect(f"http://{PORTAL_IP}:{PORTAL_PORT}/")


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=PORTAL_PORT, debug=False)