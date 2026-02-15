#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import random
import threading
import base64
from datetime import datetime
from typing import Optional, Dict, Any, Tuple, List

import requests
import pytz
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv, dotenv_values, set_key

app = Flask(__name__)

# =============================
# Paths
# =============================
APP_DIR = "/tmp/mawared_data"
os.makedirs(APP_DIR, exist_ok=True)

TOKEN_FILE = os.path.join(APP_DIR, "token.txt")
TOKEN_BACKUP_FILE = os.path.join(APP_DIR, "token_backup.txt")
INFO_FILE = os.path.join(APP_DIR, "mawared_settings.json")
AUTO_FILE = os.path.join(APP_DIR, "auto.json")
LOG_FILE = os.path.join(APP_DIR, "system.log")
ENV_FILE = os.path.join(APP_DIR, ".env")

# =============================
# Load env
# =============================
load_dotenv(override=True)

KSA_TZ = pytz.timezone("Asia/Riyadh")

# =============================
# Mawared constants
# =============================
BASE_URL = "https://mawared.moh.gov.sa"
EMPLOYEE_INFO_URL = f"{BASE_URL}/api/employee/info"
ATTENDANCE_URL = f"{BASE_URL}/api/employee/attendance"

API_CODE = "C6252BF3-A5F9-4209-8691-15E1B02A9911"
USER_AGENT = "Mawared/5.4.8 (sa.gov.moh; iOS)"

def env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")

SHOW_TOKEN_MANAGEMENT = env_bool("SHOW_TOKEN_MANAGEMENT", False)
UPDATE_TOKEN_APIKEY = str(os.environ.get("UPDATE_TOKEN_APIKEY", "")).strip()

# =============================
# Logging
# =============================
log_lock = threading.Lock()

def log(msg: str):
    ts = datetime.now(KSA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {msg}"
    with log_lock:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    print(line, flush=True)

# =============================
# Token helpers
# =============================
_token_cache = {"token": "", "timestamp": 0, "ttl": 3600}
_token_lock = threading.Lock()

def encrypt_token(token: str) -> str:
    return base64.b64encode(token.encode()).decode()

def decrypt_token(token: str) -> str:
    return base64.b64decode(token.encode()).decode()

def set_token(token: str):
    if not token:
        return
    enc = encrypt_token(token)

    with open(TOKEN_FILE, "w") as f:
        f.write(enc)
    with open(TOKEN_BACKUP_FILE, "w") as f:
        f.write(enc)

    set_key(ENV_FILE, "MAWARED_TOKEN", token)

    with _token_lock:
        _token_cache["token"] = token
        _token_cache["timestamp"] = time.time()

def read_token_file(path: str) -> str:
    try:
        if os.path.exists(path):
            return decrypt_token(open(path).read().strip())
    except Exception:
        pass
    return ""

def get_token() -> str:
    with _token_lock:
        if _token_cache["token"] and time.time() - _token_cache["timestamp"] < _token_cache["ttl"]:
            return _token_cache["token"]

    token = os.environ.get("MAWARED_TOKEN", "").strip()
    if not token:
        token = read_token_file(TOKEN_FILE)
    if not token:
        token = read_token_file(TOKEN_BACKUP_FILE)

    if token:
        with _token_lock:
            _token_cache["token"] = token
            _token_cache["timestamp"] = time.time()
    else:
        log("❌ لم يتم العثور على توكن صالح في أي مصدر")

    return token

# =============================
# Mawared requests
# =============================
def build_headers(token: str) -> Dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "apiCode": API_CODE   # ✅ التعديل المهم هنا
    }

def fetch_employee_info(token: str):
    try:
        r = requests.get(EMPLOYEE_INFO_URL, headers=build_headers(token), timeout=20)
        return r.status_code == 200, r.status_code, r.json()
    except Exception as e:
        return False, str(e), {}

def send_attendance(token: str, action: str):
    try:
        payload = {"type": action}
        r = requests.post(
            ATTENDANCE_URL,
            headers=build_headers(token),
            json=payload,
            timeout=20
        )
        return r.status_code == 200, r.status_code, r.json()
    except Exception as e:
        return False, str(e), {}

# =============================
# Routes
# =============================
@app.route("/health")
def health():
    token = get_token()
    return jsonify({
        "status": "ok",
        "time": datetime.now(KSA_TZ).isoformat(),
        "token_exists": bool(token)
    })

@app.route("/status")
def status():
    token = get_token()
    if not token:
        return jsonify({"token_status": False, "message": "لا يوجد توكن"})
    ok, code, _ = fetch_employee_info(token)
    return jsonify({
        "token_status": ok,
        "http_code": code
    })

@app.route("/employee-info")
def employee_info():
    token = get_token()
    if not token:
        return jsonify({"ok": False, "message": "لا يوجد توكن"}), 400
    ok, code, data = fetch_employee_info(token)
    return jsonify({"ok": ok, "code": code, "data": data})

@app.route("/updateToken", methods=["POST"])
def update_token():
    body = request.get_json(silent=True) or {}
    token = body.get("token", "")

    if not SHOW_TOKEN_MANAGEMENT:
        if UPDATE_TOKEN_APIKEY:
            key = request.headers.get("x-api-key") or body.get("apikey")
            if key != UPDATE_TOKEN_APIKEY:
                return jsonify({"ok": False, "message": "APIKEY غير صحيح"}), 403
        else:
            return jsonify({"ok": False, "message": "إدارة التوكن مخفية"}), 403

    if not token:
        return jsonify({"ok": False, "message": "التوكن فارغ"}), 400

    set_token(token)
    log("✅ تم تحديث التوكن بنجاح")
    return jsonify({"ok": True})

# =============================
# Main
# =============================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    log(f"🚀 Server started on port {port}")
    app.run(host="0.0.0.0", port=port)
