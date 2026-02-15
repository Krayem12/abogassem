#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ==============================
# MAWARED PYTHON PRO – CLOUD RUN EDITION (NO INTERNAL SCHEDULER)
# مع حماية أوقات الدوام للـتحضير الآلي فقط
# الإصدار: 5.5.0 (مع إصلاحات المسارات) + Patch: updateToken 403 fix (APIKEY/ENV)
# ==============================

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

# ------------------------------
# إعدادات المسارات
# ------------------------------
APP_DIR = os.path.join(os.getcwd(), "mawared_data")
os.makedirs(APP_DIR, exist_ok=True)

TOKEN_FILE = os.path.join(APP_DIR, "token.txt")
TOKEN_BACKUP_FILE = os.path.join(APP_DIR, "token_backup.txt")
INFO_FILE = os.path.join(APP_DIR, "mawared_settings.json")
AUTO_FILE = os.path.join(APP_DIR, "auto.json")
LOG_FILE = os.path.join(APP_DIR, "system.log")
ENV_FILE = os.path.join(os.getcwd(), ".env")

# ------------------------------
# تحميل .env إذا وجد
# ------------------------------
try:
    load_dotenv(override=True)
except Exception:
    pass

# ------------------------------
# المنطقة الزمنية
# ------------------------------
KSA_TZ = pytz.timezone("Asia/Riyadh")

# ------------------------------
# ثوابت Mawared
# ------------------------------
BASE_URL = "https://mawared.moh.gov.sa"
LOGIN_URL = f"{BASE_URL}/api/account/login"
EMPLOYEE_INFO_URL = f"{BASE_URL}/api/employee/info"
ATTENDANCE_URL = f"{BASE_URL}/api/employee/attendance"

API_CODE = "C6252BF3-A5F9-4209-8691-15E1B02A9911"
USER_AGENT = "Mawared/5.4.8 (sa.gov.moh; build:1; iOS 26.0.1) Alamofire/5.10.2"

def env_bool(name: str, default: bool = False) -> bool:
    """
    قراءة متغيرات البيئة كقيمة منطقية بشكل متسامح.
    يقبل: true/1/yes/on (بأي حالة أحرف)، ويتجاهل الاقتباسات والمسافات.
    """
    val = os.environ.get(name, None)
    if val is None:
        return default
    val = str(val).strip().strip('"').strip("'").lower()
    return val in ("1", "true", "yes", "y", "on")

# إعدادات تليجرام
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# إدارة التوكن (كما كان سابقاً)
SHOW_TOKEN_MANAGEMENT = env_bool("SHOW_TOKEN_MANAGEMENT", False)

# ✅ Patch: API Key خاص بتحديث التوكن (بديل آمن عن فتح الإدارة كاملة)
# إذا تم تعيينه -> يسمح لـ /updateToken حتى لو SHOW_TOKEN_MANAGEMENT=False
UPDATE_TOKEN_APIKEY = str(os.environ.get("UPDATE_TOKEN_APIKEY", "")).strip()

# ------------------------------
# إعدادات الدوام الافتراضية
# ------------------------------
DEFAULT_START_MIN = 7 * 60 + 30   # 07:30
DEFAULT_END_MIN = 15 * 60 + 30    # 15:30

# نافذة السماح الافتراضية (مثل 10 دقائق قبل/بعد)
DEFAULT_WINDOW_MIN = 10

# نافذة توليد الأوقات (التحضير)
PREP_START_MIN = 7 * 60
PREP_END_MIN = 8 * 60

# ------------------------------
# تحميل إعدادات المستخدم إن وجدت
# ------------------------------
def safe_int(val, default):
    try:
        return int(val)
    except Exception:
        return default

def load_settings() -> Dict[str, Any]:
    data = {}
    try:
        if os.path.exists(INFO_FILE):
            with open(INFO_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
    except Exception:
        data = {}

    merged = {
        "start_min": safe_int(data.get("start_min"), DEFAULT_START_MIN),
        "end_min": safe_int(data.get("end_min"), DEFAULT_END_MIN),
        "window_min": safe_int(data.get("window_min"), DEFAULT_WINDOW_MIN),

        "prep_start_min": safe_int(data.get("prep_start_min"), PREP_START_MIN),
        "prep_end_min": safe_int(data.get("prep_end_min"), PREP_END_MIN),

        "auto_enabled": bool(data.get("auto_enabled", True)),
        "auto_checkin_enabled": bool(data.get("auto_checkin_enabled", True)),
        "auto_checkout_enabled": bool(data.get("auto_checkout_enabled", True)),

        "checkin_start": safe_int(data.get("checkin_start"), None),
        "checkin_end": safe_int(data.get("checkin_end"), None),
        "checkout_start": safe_int(data.get("checkout_start"), None),
        "checkout_end": safe_int(data.get("checkout_end"), None),

        "randomize_minutes": safe_int(data.get("randomize_minutes"), 0),
        "holiday_block": bool(data.get("holiday_block", True)),
        "weekend_block": bool(data.get("weekend_block", True)),
        "notify_all": bool(data.get("notify_all", True)),
    }
    return merged

settings_lock = threading.Lock()
settings = load_settings()

# ------------------------------
# سجل النظام
# ------------------------------
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

# ------------------------------
# حالة الجدولة اليومية للـ Auto
# ------------------------------
auto_state = {
    "date": "",
    "done_in": False,
    "done_out": False,
    "last_msg_date": "",
    "last_holiday_msg_date": "",
    "scheduler_paused": False
}
auto_state_lock = threading.Lock()

# ------------------------------
# كاش التوكن
# ------------------------------
_token_cache = {
    "token": "",
    "timestamp": 0,
    "ttl": 3600
}
_token_cache_lock = threading.Lock()

def clear_token_cache():
    with _token_cache_lock:
        _token_cache["token"] = ""
        _token_cache["timestamp"] = 0
        _token_cache["ttl"] = 3600
    log("🗑️ تم مسح كاش التوكن")

# ------------------------------
# دوال مساعدة
# ------------------------------
def now_ksa() -> datetime:
    return datetime.now(KSA_TZ)

def minutes_now() -> int:
    n = now_ksa()
    return n.hour * 60 + n.minute

def fmt_minutes(m: int) -> str:
    if m is None:
        return "--:--"
    h = m // 60
    mm = m % 60
    return f"{h:02d}:{mm:02d}"

def is_weekend(dt: datetime) -> bool:
    return dt.weekday() in (4, 5)

# ------------------------------
# تليجرام
# ------------------------------
def telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log(f"⚠️ خطأ تليجرام: {e}")

# ------------------------------
# تشفير/فك تشفير التوكن (Base64)
# ------------------------------
def encrypt_token(token: str) -> str:
    try:
        b = token.encode("utf-8")
        return base64.b64encode(b).decode("utf-8")
    except Exception:
        return token

def decrypt_token(enc: str) -> str:
    try:
        b = base64.b64decode(enc.encode("utf-8"))
        return b.decode("utf-8")
    except Exception:
        return enc

# ------------------------------
# قراءة/كتابة التوكن
# ------------------------------
def write_env_token(token: str):
    try:
        if not os.path.exists(ENV_FILE):
            with open(ENV_FILE, "w") as f:
                f.write("")
        set_key(ENV_FILE, "MAWARED_TOKEN", token)
    except Exception as e:
        log(f"⚠️ تعذر تحديث .env: {e}")

def set_token(token: str):
    if not token:
        return

    write_env_token(token)
    enc = encrypt_token(token)

    with _token_cache_lock:
        _token_cache["token"] = token
        _token_cache["timestamp"] = time.time()
        _token_cache["ttl"] = 3600

    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(enc)
    except Exception as e:
        log(f"⚠️ تعذر حفظ token.txt: {e}")

    try:
        with open(TOKEN_BACKUP_FILE, "w", encoding="utf-8") as f:
            f.write(enc)
    except Exception as e:
        log(f"⚠️ تعذر حفظ token_backup.txt: {e}")

def read_file_token(path: str) -> str:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = f.read().strip()
            if data:
                return decrypt_token(data)
    except Exception:
        pass
    return ""

def read_env_token() -> str:
    try:
        if os.path.exists(ENV_FILE):
            envs = dotenv_values(ENV_FILE)
            t = envs.get("MAWARED_TOKEN", "")
            if t:
                return str(t).strip()
    except Exception:
        pass
    return ""

def get_token() -> str:
    global _token_cache

    current_time = time.time()
    with _token_cache_lock:
        if (_token_cache["token"] and
            current_time - _token_cache["timestamp"] < _token_cache["ttl"]):
            return _token_cache["token"]

    token = ""

    # 1) Environment
    token = os.environ.get("MAWARED_TOKEN", "").strip()
    if token:
        log("🔑 تم جلب التوكن من Environment")
    else:
        # 2) .env
        token = read_env_token()
        if token:
            log("🔑 تم جلب التوكن من .env")
        else:
            # 3) token.txt
            token = read_file_token(TOKEN_FILE)
            if token:
                log("🔑 تم جلب التوكن من token.txt")
            else:
                # 4) token_backup.txt
                token = read_file_token(TOKEN_BACKUP_FILE)
                if token:
                    log("🔑 تم جلب التوكن من token_backup.txt")

    if not token:
        log("❌ لم يتم العثور على توكن صالح في أي مصدر")
    else:
        with _token_cache_lock:
            _token_cache["token"] = token
            _token_cache["timestamp"] = current_time
            _token_cache["ttl"] = 3600

    return token

# ------------------------------
# طلبات Mawared
# ------------------------------
def build_headers(token: str) -> Dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "api_code": API_CODE
    }

def fetch_employee_info(token: str) -> Tuple[bool, str, Dict[str, Any]]:
    try:
        headers = build_headers(token)
        r = requests.get(EMPLOYEE_INFO_URL, headers=headers, timeout=20)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}", {}
        data = r.json()
        return True, "OK", data
    except Exception as e:
        return False, str(e), {}

def send_attendance(token: str, action: str) -> Tuple[bool, str, Dict[str, Any]]:
    try:
        headers = build_headers(token)
        payload = {"type": action}
        r = requests.post(ATTENDANCE_URL, headers=headers, json=payload, timeout=20)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}", {}
        data = r.json()
        return True, "OK", data
    except Exception as e:
        return False, str(e), {}

# ------------------------------
# أدوات حضور/انصراف
# ------------------------------
def is_holiday_blocked(employee_data: Dict[str, Any]) -> bool:
    if not employee_data:
        return False
    try:
        for k in ("isOnLeave", "onLeave", "holiday", "isHoliday", "hasLeave"):
            if k in employee_data and bool(employee_data.get(k)):
                return True

        emp = employee_data.get("employee") or employee_data.get("data") or {}
        for k in ("isOnLeave", "onLeave", "holiday", "isHoliday", "hasLeave"):
            if k in emp and bool(emp.get(k)):
                return True
    except Exception:
        pass
    return False

def load_auto_json() -> Dict[str, Any]:
    try:
        if os.path.exists(AUTO_FILE):
            with open(AUTO_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_auto_json(data: Dict[str, Any]):
    try:
        with open(AUTO_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"⚠️ تعذر حفظ auto.json: {e}")

def load_auto_state():
    data = load_auto_json()
    with auto_state_lock:
        auto_state["date"] = data.get("date", "")
        auto_state["done_in"] = bool(data.get("done_in", False))
        auto_state["done_out"] = bool(data.get("done_out", False))
        auto_state["last_msg_date"] = data.get("last_msg_date", "")
        auto_state["last_holiday_msg_date"] = data.get("last_holiday_msg_date", "")
        auto_state["scheduler_paused"] = bool(data.get("scheduler_paused", False))

def persist_auto_state():
    with auto_state_lock:
        data = dict(auto_state)
    save_auto_json(data)

def reset_auto_state_if_new_day():
    today = now_ksa().strftime("%Y-%m-%d")
    with auto_state_lock:
        if auto_state["date"] != today:
            auto_state["date"] = today
            auto_state["done_in"] = False
            auto_state["done_out"] = False
            auto_state["last_msg_date"] = ""
            auto_state["last_holiday_msg_date"] = ""
            log(f"🆕 يوم جديد: إعادة ضبط حالة الأوتو لليوم {today}")
    persist_auto_state()

# ------------------------------
# فحص التوكن
# ------------------------------
def validate_token() -> Tuple[bool, str]:
    token = get_token()
    if not token:
        return False, "لا يوجد توكن"
    ok, msg, _ = fetch_employee_info(token)
    if not ok:
        return False, f"توكن غير صالح: {msg}"
    return True, "توكن صالح"

# ------------------------------
# تحضير/توليد أوقات اليوم
# ------------------------------
def rand_offset_minutes() -> int:
    with settings_lock:
        r = int(settings.get("randomize_minutes", 0) or 0)
    if r <= 0:
        return 0
    return random.randint(0, r)

def within_window(now_min: int, start_min: int, end_min: int, window_min: int) -> bool:
    return (start_min - window_min) <= now_min <= (end_min + window_min)

def get_checkin_window() -> Tuple[int, int]:
    with settings_lock:
        s = settings.get("checkin_start")
        e = settings.get("checkin_end")
        start_min = settings.get("start_min", DEFAULT_START_MIN)
        window_min = settings.get("window_min", DEFAULT_WINDOW_MIN)

    if s is None or e is None:
        return start_min, start_min + window_min
    return int(s), int(e)

def get_checkout_window() -> Tuple[int, int]:
    with settings_lock:
        s = settings.get("checkout_start")
        e = settings.get("checkout_end")
        end_min = settings.get("end_min", DEFAULT_END_MIN)
        window_min = settings.get("window_min", DEFAULT_WINDOW_MIN)

    if s is None or e is None:
        return end_min, end_min + window_min
    return int(s), int(e)

def generate_daily_times_at_7am():
    try:
        reset_auto_state_if_new_day()

        today_str = datetime.now(KSA_TZ).strftime("%Y-%m-%d")
        weekday = datetime.now(KSA_TZ).weekday()
        now = datetime.now(KSA_TZ)
        if now.hour != 7 and not env_bool("ALLOW_TIME_GENERATION_ANYTIME", False):
            log(f"⏸️ GENERATE_TIMES_7AM: تم استدعاء التوليد عند {now.strftime('%H:%M')} وليس 07:00 - تخطي")
            return

        if weekday in (4, 5):
            log("📅 اليوم عطلة (جمعة/سبت) - لن يتم توليد أوقات")
            return

        with settings_lock:
            last_gen = settings.get("last_generation_date", "")
        if last_gen == today_str:
            log(f"✅ تم توليد أوقات اليوم مسبقاً بتاريخ {today_str}")
            return

        with settings_lock:
            start_min = int(settings.get("start_min", DEFAULT_START_MIN))
            end_min = int(settings.get("end_min", DEFAULT_END_MIN))
            window_min = int(settings.get("window_min", DEFAULT_WINDOW_MIN))
            prep_start = int(settings.get("prep_start_min", PREP_START_MIN))
            prep_end = int(settings.get("prep_end_min", PREP_END_MIN))

        cin_start = start_min + rand_offset_minutes()
        cin_end = cin_start + window_min

        cout_start = end_min + rand_offset_minutes()
        cout_end = cout_start + window_min

        with settings_lock:
            settings["checkin_start"] = cin_start
            settings["checkin_end"] = cin_end
            settings["checkout_start"] = cout_start
            settings["checkout_end"] = cout_end
            settings["prep_start_min"] = prep_start
            settings["prep_end_min"] = prep_end
            settings["last_generation_date"] = today_str

        try:
            with open(INFO_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log(f"⚠️ تعذر حفظ mawared_settings.json: {e}")

        msg = (
            f"🕖 تم توليد أوقات اليوم ({today_str})\n"
            f"🟢 دخول: {fmt_minutes(cin_start)} - {fmt_minutes(cin_end)}\n"
            f"🔴 خروج: {fmt_minutes(cout_start)} - {fmt_minutes(cout_end)}\n"
            f"⏱️ تحضير: {fmt_minutes(prep_start)} - {fmt_minutes(prep_end)}"
        )
        log(msg)
        if weekday not in (4, 5):
            telegram(msg)

    except Exception as e:
        log(f"❌ خطأ في توليد أوقات الساعة 7: {e}")

# ------------------------------
# تنفيذ الحضور/الانصراف
# ------------------------------
def perform_attendance(action: str) -> Tuple[bool, str]:
    token = get_token()
    if not token:
        return False, "لا يوجد توكن"

    ok, msg, info = fetch_employee_info(token)
    if not ok:
        if "HTTP 401" in msg:
            clear_token_cache()
        return False, f"فشل جلب بيانات الموظف: {msg}"

    with settings_lock:
        holiday_block = bool(settings.get("holiday_block", True))
        weekend_block = bool(settings.get("weekend_block", True))

    if weekend_block and is_weekend(now_ksa()):
        return False, "اليوم عطلة (جمعة/سبت) - تم منع الإجراء"

    if holiday_block and is_holiday_blocked(info):
        return False, "يوجد إجازة/مانع - تم منع الإجراء"

    ok2, msg2, _ = send_attendance(token, action)
    if not ok2:
        if "HTTP 401" in msg2:
            clear_token_cache()
        return False, f"فشل إرسال الطلب: {msg2}"

    return True, "تم بنجاح"

# ------------------------------
# مهمة الأوتو (يتم استدعاؤها من Cloud Scheduler / كرون خارجي)
# ------------------------------
AUTO_CHECKIN_START = os.environ.get("AUTO_CHECKIN_START", "")
AUTO_CHECKIN_END = os.environ.get("AUTO_CHECKIN_END", "")
AUTO_CHECKOUT_START = os.environ.get("AUTO_CHECKOUT_START", "")
AUTO_CHECKOUT_END = os.environ.get("AUTO_CHECKOUT_END", "")

def parse_hhmm(s: str) -> Optional[int]:
    try:
        s = (s or "").strip()
        if not s:
            return None
        hh, mm = s.split(":")
        return int(hh) * 60 + int(mm)
    except Exception:
        return None

def get_auto_window_from_env_or_settings(kind: str) -> Tuple[int, int]:
    if kind == "in":
        s_env = parse_hhmm(AUTO_CHECKIN_START)
        e_env = parse_hhmm(AUTO_CHECKIN_END)
        if s_env is not None and e_env is not None:
            return s_env, e_env
        return get_checkin_window()

    s_env = parse_hhmm(AUTO_CHECKOUT_START)
    e_env = parse_hhmm(AUTO_CHECKOUT_END)
    if s_env is not None and e_env is not None:
        return s_env, e_env
    return get_checkout_window()

def auto_check_job():
    try:
        load_auto_state()
        reset_auto_state_if_new_day()

        now = now_ksa()
        weekday = now.weekday()
        nmin = minutes_now()

        with settings_lock:
            auto_enabled = bool(settings.get("auto_enabled", True))
            auto_in_enabled = bool(settings.get("auto_checkin_enabled", True))
            auto_out_enabled = bool(settings.get("auto_checkout_enabled", True))
            weekend_block = bool(settings.get("weekend_block", True))

        with auto_state_lock:
            scheduler_paused = auto_state.get("scheduler_paused", False)

        if not auto_enabled or scheduler_paused:
            log("⏸️ AUTO_JOB: الأوتو متوقف")
            return

        if weekend_block and weekday in (4, 5):
            log("📅 AUTO_JOB: عطلة (جمعة/سبت) - لا يوجد تنفيذ")
            return

        if now.hour == 7:
            generate_daily_times_at_7am()

        with auto_state_lock:
            done_in = bool(auto_state.get("done_in", False))
            done_out = bool(auto_state.get("done_out", False))

        if auto_in_enabled and not done_in:
            s, e = get_auto_window_from_env_or_settings("in")
            if not within_window(nmin, s, e, 0):
                msg = (
                    f"⛔ ممنوع تسجيل الدخول الآن\n"
                    f"الوقت الحالي: {fmt_minutes(nmin)}\n"
                    f"نافذة الدخول: {fmt_minutes(s)} - {fmt_minutes(e)}"
                )
                log(msg)
                if weekday not in (4, 5):
                    telegram(msg)
            else:
                log("🟢 AUTO_JOB: بدء تسجيل الدخول الآلي...")
                ok, message = perform_attendance("checkin")
                with auto_state_lock:
                    if ok:
                        auto_state["done_in"] = True
                status_msg = f"🟢 الدخول الآلي - {message}"
                log(f"✅ AUTO_JOB: {status_msg}")
                if weekday not in (4, 5):
                    telegram(status_msg)
                persist_auto_state()

        if auto_out_enabled and not done_out:
            s, e = get_auto_window_from_env_or_settings("out")
            if not within_window(nmin, s, e, 0):
                msg = (
                    f"⛔ ممنوع تسجيل الخروج الآن\n"
                    f"الوقت الحالي: {fmt_minutes(nmin)}\n"
                    f"نافذة الخروج: {fmt_minutes(s)} - {fmt_minutes(e)}"
                )
                log(msg)
                if weekday not in (4, 5):
                    telegram(msg)
            else:
                log("🔴 AUTO_JOB: بدء تسجيل الخروج الآلي...")
                ok, message = perform_attendance("checkout")
                with auto_state_lock:
                    if ok:
                        auto_state["done_out"] = True
                status_msg = f"🔴 الخروج الآلي - {message}"
                log(f"✅ AUTO_JOB: {status_msg}")
                if weekday not in (4, 5):
                    telegram(status_msg)
                persist_auto_state()

    except Exception as e:
        log(f"❌ AUTO_JOB خطأ: {e}")

# ------------------------------
# دوال مساعدة للمسارات الجديدة
# ------------------------------
def get_employee_history() -> List[Dict[str, Any]]:
    token = get_token()
    if not token:
        return []
    ok, msg, data = fetch_employee_info(token)
    if not ok:
        return []
    history = []
    if isinstance(data, dict):
        for key in ["attendance", "transactions", "history", "records"]:
            if key in data and isinstance(data[key], list):
                history = data[key]
                break
        if not history and "data" in data and isinstance(data["data"], dict):
            for key in ["attendance", "transactions", "history", "records"]:
                if key in data["data"] and isinstance(data["data"][key], list):
                    history = data["data"][key]
                    break
    return history

# ------------------------------
# ✅ Patch helpers: APIKEY check + simple CORS for /updateToken
# ------------------------------
def _extract_api_key_from_request(body: Dict[str, Any]) -> str:
    # from header
    hdr = request.headers.get("x-api-key", "") or request.headers.get("X-API-KEY", "")
    hdr = str(hdr).strip()
    if hdr:
        return hdr
    # from json
    return str(body.get("apikey", "")).strip()

def _corsify(resp):
    # Minimal CORS (helps if any future frontend hits it)
    try:
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, x-api-key"
    except Exception:
        pass
    return resp

# ------------------------------
# Flask Routes
# ------------------------------
@app.route("/")
def home():
    return send_from_directory(os.getcwd(), "index.html")

@app.route("/health")
def health():
    token_ok, token_msg = validate_token()
    with settings_lock:
        auto_enabled = settings.get("auto_enabled", True)
    with auto_state_lock:
        scheduler_paused = auto_state.get("scheduler_paused", False)
    return jsonify({
        "status": "ok",
        "time": now_ksa().isoformat(),
        "auto_enabled": auto_enabled,
        "scheduler_paused": scheduler_paused,
        "token_status": "exists" if token_ok else "missing"
    })

@app.route("/status")
def status():
    token_ok, token_msg = validate_token()
    with settings_lock:
        s = dict(settings)
    with auto_state_lock:
        a = dict(auto_state)
    return jsonify({
        "token_status": token_ok,
        "token_message": token_msg,
        "settings": s,
        "auto_state": a
    })

@app.route("/employee-info")
def employee_info():
    token = get_token()
    if not token:
        return jsonify({"ok": False, "message": "لا يوجد توكن"}), 400
    ok, msg, data = fetch_employee_info(token)
    return jsonify({"ok": ok, "message": msg, "data": data})

@app.route("/set-token", methods=["POST"])
def set_token_route():
    if not SHOW_TOKEN_MANAGEMENT:
        return jsonify({"ok": False, "message": "إدارة التوكن مخفية"}), 403

    body = request.get_json(silent=True) or {}
    token = str(body.get("token", "")).strip()
    if not token:
        return jsonify({"ok": False, "message": "التوكن فارغ"}), 400
    set_token(token)
    return jsonify({"ok": True, "message": "تم تحديث التوكن"})

@app.route("/clear-token", methods=["POST"])
def clear_token_route():
    if not SHOW_TOKEN_MANAGEMENT:
        return jsonify({"ok": False, "message": "إدارة التوكن مخفية"}), 403
    clear_token_cache()
    try:
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
        if os.path.exists(TOKEN_BACKUP_FILE):
            os.remove(TOKEN_BACKUP_FILE)
    except Exception:
        pass
    return jsonify({"ok": True, "message": "تم مسح التوكن"})

@app.route("/save-settings", methods=["POST"])
def save_settings_route():
    body = request.get_json(silent=True) or {}
    with settings_lock:
        for k in [
            "start_min", "end_min", "window_min",
            "prep_start_min", "prep_end_min",
            "auto_enabled", "auto_checkin_enabled", "auto_checkout_enabled",
            "checkin_start", "checkin_end", "checkout_start", "checkout_end",
            "randomize_minutes", "holiday_block", "weekend_block", "notify_all"
        ]:
            if k in body:
                settings[k] = body[k]

        for k in ["start_min","end_min","window_min","prep_start_min","prep_end_min",
                  "checkin_start","checkin_end","checkout_start","checkout_end","randomize_minutes"]:
            if k in settings and settings[k] is not None:
                settings[k] = safe_int(settings[k], settings.get(k))

    try:
        with open(INFO_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500

    return jsonify({"ok": True, "message": "تم حفظ الإعدادات", "settings": settings})

@app.route("/schedule")
def schedule_view():
    with settings_lock:
        cin_s = settings.get("checkin_start")
        cin_e = settings.get("checkin_end")
        cout_s = settings.get("checkout_start")
        cout_e = settings.get("checkout_end")
        prep_s = settings.get("prep_start_min")
        prep_e = settings.get("prep_end_min")
        last = settings.get("last_generation_date", "")
        auto_enabled = settings.get("auto_enabled", True)

    with auto_state_lock:
        today_state = {
            "done_in": auto_state.get("done_in", False),
            "done_out": auto_state.get("done_out", False),
            "date": auto_state.get("date", "")
        }
        scheduler_paused = auto_state.get("scheduler_paused", False)

    return jsonify({
        "last_generation_date": last,
        "enabled": auto_enabled,
        "scheduler_paused": scheduler_paused,
        "today": today_state,
        "checkin": {
            "start": cin_s,
            "end": cin_e,
            "start_str": fmt_minutes(cin_s) if cin_s else None,
            "end_str": fmt_minutes(cin_e) if cin_e else None
        },
        "checkout": {
            "start": cout_s,
            "end": cout_e,
            "start_str": fmt_minutes(cout_s) if cout_s else None,
            "end_str": fmt_minutes(cout_e) if cout_e else None
        },
        "prep": {
            "start": prep_s,
            "end": prep_e,
            "start_str": fmt_minutes(prep_s),
            "end_str": fmt_minutes(prep_e)
        }
    })

@app.route("/auto-job", methods=["GET", "POST"])
def auto_job_route():
    auto_check_job()
    return jsonify({"ok": True, "message": "AUTO_JOB executed"})

@app.route("/autoon", methods=["POST"])
def auto_on():
    with settings_lock:
        settings["auto_enabled"] = True
    try:
        with open(INFO_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500
    log("🟢 تم تفعيل النظام الآلي يدويًا")
    telegram("🟢 تم تفعيل النظام الآلي")
    return jsonify({"ok": True, "message": "تم تفعيل النظام الآلي"})

@app.route("/autooff", methods=["POST"])
def auto_off():
    with settings_lock:
        settings["auto_enabled"] = False
    try:
        with open(INFO_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500
    log("🔴 تم إيقاف النظام الآلي يدويًا")
    telegram("🔴 تم إيقاف النظام الآلي")
    return jsonify({"ok": True, "message": "تم إيقاف النظام الآلي"})

@app.route("/force-auto-check", methods=["GET", "POST"])
def force_auto_check():
    auto_check_job()
    return jsonify({"ok": True, "message": "تم تنفيذ التحقق الآلي"})

@app.route("/generate-daily-times", methods=["POST"])
def generate_daily_times():
    generate_daily_times_at_7am()
    return jsonify({"ok": True, "message": "تم توليد الأوقات اليومية"})

@app.route("/reset-auto-state", methods=["POST"])
def reset_auto_state_route():
    with auto_state_lock:
        auto_state["done_in"] = False
        auto_state["done_out"] = False
        auto_state["date"] = now_ksa().strftime("%Y-%m-%d")
    persist_auto_state()
    log("🔄 تم إعادة تعيين حالة التشغيل الآلي")
    telegram("🔄 تم إعادة تعيين حالة التشغيل الآلي")
    return jsonify({"ok": True, "message": "تم إعادة تعيين الحالة"})

# ✅ Patch: OPTIONS support (preflight)
@app.route("/updateToken", methods=["POST", "OPTIONS"])
def update_token_route():
    if request.method == "OPTIONS":
        return _corsify(jsonify({"ok": True}))

    body = request.get_json(silent=True) or {}
    token = str(body.get("token", "")).strip()

    # ✅ Allow via SHOW_TOKEN_MANAGEMENT OR via UPDATE_TOKEN_APIKEY
    if not SHOW_TOKEN_MANAGEMENT:
        if not UPDATE_TOKEN_APIKEY:
            # لا إدارة توكن، ولا APIKEY مضبوط
            resp = jsonify({"ok": False, "message": "إدارة التوكن مخفية (فعّل SHOW_TOKEN_MANAGEMENT أو ضع UPDATE_TOKEN_APIKEY)"})
            return _corsify(resp), 403

        incoming_key = _extract_api_key_from_request(body)
        if not incoming_key or incoming_key != UPDATE_TOKEN_APIKEY:
            resp = jsonify({"ok": False, "message": "APIKEY غير صحيح"})
            return _corsify(resp), 403

    if not token:
        resp = jsonify({"ok": False, "message": "التوكن فارغ"})
        return _corsify(resp), 400

    set_token(token)
    log("✅ تم تحديث التوكن عبر /updateToken")
    resp = jsonify({"ok": True, "message": "تم تحديث التوكن"})
    return _corsify(resp)

@app.route("/getToken", methods=["GET"])
def get_token_route():
    if not SHOW_TOKEN_MANAGEMENT:
        return jsonify({"ok": False, "message": "إدارة التوكن مخفية"}), 403
    token = get_token()
    if token:
        return jsonify({"ok": True, "token": token, "length": len(token)})
    else:
        return jsonify({"ok": False, "message": "لا يوجد توكن"})

@app.route("/debug-token", methods=["GET"])
def debug_token_route():
    if not SHOW_TOKEN_MANAGEMENT:
        return jsonify({"ok": False, "message": "إدارة التوكن مخفية"}), 403
    token_file_exists = os.path.exists(TOKEN_FILE)
    backup_file_exists = os.path.exists(TOKEN_BACKUP_FILE)
    info_file_exists = os.path.exists(INFO_FILE)
    auto_file_exists = os.path.exists(AUTO_FILE)
    current_token = get_token()
    env_token_exists = bool(os.environ.get("MAWARED_TOKEN")) or bool(read_env_token())
    return jsonify({
        "token_file_exists": token_file_exists,
        "backup_file_exists": backup_file_exists,
        "info_file_exists": info_file_exists,
        "auto_file_exists": auto_file_exists,
        "current_token_length": len(current_token) if current_token else 0,
        "environment_token_exists": env_token_exists
    })

@app.route("/fix-token", methods=["POST"])
def fix_token_route():
    if not SHOW_TOKEN_MANAGEMENT:
        return jsonify({"ok": False, "message": "إدارة التوكن مخفية"}), 403
    recovered = False
    token = ""
    token = read_file_token(TOKEN_BACKUP_FILE)
    if token:
        set_token(token)
        recovered = True
    else:
        token = read_file_token(TOKEN_FILE)
        if token:
            set_token(token)
            recovered = True
    if recovered:
        return jsonify({"ok": True, "recovered": True, "token_length": len(token)})
    else:
        return jsonify({"ok": False, "recovered": False, "message": "لم يتم العثور على توكن في النسخ الاحتياطية"})

@app.route("/config", methods=["GET"])
def config_route():
    return jsonify({
        "show_token_management": SHOW_TOKEN_MANAGEMENT,
        "update_token_apikey_set": bool(UPDATE_TOKEN_APIKEY)
    })

@app.route("/history", methods=["GET"])
def history_route():
    history = get_employee_history()
    return jsonify({
        "ok": True,
        "records": history
    })

@app.route("/check", methods=["POST"])
def check_route():
    return checkin_route()

@app.route("/checkin", methods=["POST"])
def checkin_route():
    ok, msg = perform_attendance("checkin")
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)

@app.route("/checkout", methods=["POST"])
def checkout_route():
    ok, msg = perform_attendance("checkout")
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)

# ------------------------------
# تشغيل التطبيق
# ------------------------------
if __name__ == "__main__":
    try:
        load_auto_state()
        reset_auto_state_if_new_day()
    except Exception:
        pass

    port = int(os.environ.get("PORT", "10000"))
    log(f"🚀 تشغيل Mawared على المنفذ: {port}")
    app.run(host="0.0.0.0", port=port)
