#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ==============================
# MAWARED PYTHON PRO – CLOUD RUN EDITION (NO INTERNAL SCHEDULER)
# مع حماية أوقات الدوام للـتحضير الآلي فقط
# الإصدار: 5.1.0 (patched: persistent holiday dedupe)
# ==============================

import os
import json
import time
import random
import threading
import base64
from datetime import datetime, timedelta

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

# تحميل المتغيرات من .env إن وجد
if os.path.exists(ENV_FILE):
    load_dotenv(ENV_FILE)

# منطقة التوقيت
KSA_TZ = pytz.timezone("Asia/Riyadh")

# إعدادات عامة - محدثة لتطابق الكود العامل
APP_VERSION = os.environ.get("APP_VERSION", "3.6.0")  # ✅ الحصول من البيئة
PLATFORM = "IOS"
API_CODE = "C6252BF3-A5F9-4209-8691-15E1B02A9911"
USER_AGENT = "Mawared/5.4.8 (sa.gov.moh; build:1; iOS 26.0.1) Alamofire/5.10.2"

# إعدادات تليجرام
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SHOW_TOKEN_MANAGEMENT = os.environ.get("SHOW_TOKEN_MANAGEMENT", "false").lower() == "true"

# حالة الجدولة اليومية للـ Auto
auto_state = {
    "date": None,
    "checkin_time": None,
    "checkout_time": None,
    "done_in": False,
    "done_out": False,
}
auto_state_lock = threading.Lock()

# حدود أوقات الدوام للحضور الآلي فقط (الأحد - الخميس)
AUTO_CHECKIN_START = "08:30"
AUTO_CHECKIN_END = "9:00"
AUTO_CHECKOUT_START = "16:00"
AUTO_CHECKOUT_END = "16:30"

# ------------------------------
# نظام منع التكرار للطلبات اليدوية
# ------------------------------
last_requests = {}
request_lock = threading.Lock()

def can_make_request(endpoint, user_id="default", cooldown_seconds=5):
    """التحقق من إمكانية إجراء الطلب (منع التكرار السريع)"""
    with request_lock:
        key = f"{endpoint}_{user_id}"
        current_time = time.time()
        last_time = last_requests.get(key, 0)

        if current_time - last_time < cooldown_seconds:
            return False

        last_requests[key] = current_time
        return True

# ------------------------------
# نظام Cache للتوكن (تحسين الأداء)
# ------------------------------
_token_cache = {
    "token": "",
    "timestamp": 0,
    "ttl": 3600  # صلاحية الكاش: ساعة واحدة
}

def clear_token_cache():
    """مسح كاش التوكن"""
    global _token_cache
    _token_cache = {"token": "", "timestamp": 0, "ttl": 3600}
    log("🗑️ تم مسح كاش التوكن")

# ------------------------------
# دوال مساعدة عامة
# ------------------------------
def is_render():
    """التعرف إذا كنا على منصة Render (للمعلومة فقط)"""
    return "RENDER" in os.environ

def log(msg: str):
    """تسجيل الرسائل في ملف السجل مع طابع زمني"""
    ts = datetime.now(KSA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def safe_log_response(text: str, prefix: str = ""):
    """تسجيل رد API بأمان (تجنب Unicode errors)"""
    try:
        safe_text = text.encode('utf-8', 'ignore').decode('utf-8', 'ignore')
        if len(safe_text) > 500:
            safe_text = safe_text[:500] + "..."
        log(f"{prefix}{safe_text}")
    except Exception as e:
        log(f"{prefix}[غير قادر على تسجيل الرد بسبب مشكلة ترميز: {str(e)}]")

def telegram(msg: str):
    """إرسال رسالة إلى تليجرام (إذا تم ضبط الإعدادات)"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("⚠️ إعدادات تليجرام غير مكتملة")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            log(f"⚠️ TELEGRAM ERROR: {resp.status_code}")
    except Exception as e:
        log(f"⚠️ TELEGRAM EXCEPTION: {str(e)}")

def save_json(path: str, data):
    """حفظ بيانات JSON في ملف"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log(f"✅ تم حفظ الملف: {path}")
    except Exception as e:
        log(f"❌ فشل حفظ {path}: {str(e)}")

def load_json(path: str, default=None):
    """قراءة ملف JSON مع إرجاع قيمة افتراضية عند الفشل"""
    if default is None:
        default = {}
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"⚠️ فشل قراءة {path}: {str(e)}")
        return default

def reset_auto_state_daily():
    """إعادة تعيين auto_state إذا تغير اليوم (تُستدعى عند بدء التشغيل)"""
    today_str = datetime.now(KSA_TZ).strftime("%Y-%m-%d")
    with auto_state_lock:
        if auto_state["date"] != today_str:
            auto_state.update({
                "date": today_str,
                "checkin_time": None,
                "checkout_time": None,
                "done_in": False,
                "done_out": False,
            })
            log(f"🔄 تم إعادة تعيين auto_state للتاريخ الجديد: {today_str}")

# ------------------------------
# منع تكرار إشعار العطلة (حل دائم لـ Cloud Run)
# ------------------------------
HOLIDAY_STATE_FILE = os.path.join(APP_DIR, "holiday_state.json")

def holiday_already_notified(today_str: str) -> bool:
    """التحقق هل تم تسجيل إشعار العطلة لهذا اليوم أم لا"""
    data = load_json(HOLIDAY_STATE_FILE, default={})
    # تنظيف البيانات القديمة (أكثر من 3 أيام)
    file_date = data.get("date")
    if file_date:
        try:
            file_date_dt = datetime.strptime(file_date, "%Y-%m-%d")
            today_dt = datetime.strptime(today_str, "%Y-%m-%d")
            if (today_dt - file_date_dt).days > 3:
                # بيانات قديمة، إزالتها
                os.remove(HOLIDAY_STATE_FILE)
                return False
        except:
            pass
    return data.get("date") == today_str

def mark_holiday_notified(today_str: str):
    """تسجيل أن إشعار العطلة تم لهذا اليوم"""
    save_json(HOLIDAY_STATE_FILE, {
        "date": today_str,
        "updated_at": datetime.now(KSA_TZ).isoformat()
    })

# ------------------------------
# تشفير / فك تشفير التوكن
# ------------------------------
def encrypt_token(token: str) -> str:
    try:
        return base64.b64encode(token.encode("utf-8")).decode("utf-8")
    except Exception as e:
        log(f"⚠️ فشل تشفير التوكن: {str(e)}")
        return token

def decrypt_token(encoded: str) -> str:
    try:
        return base64.b64decode(encoded.encode("utf-8")).decode("utf-8")
    except Exception as e:
        log(f"⚠️ فشل فك تشفير التوكن: {str(e)}")
        return encoded

# ------------------------------
# إدارة التوكن (تحميل/حفظ) مع Caching محسّن
# ------------------------------
def write_env_token(token: str):
    """تحديث متغير البيئة وملف .env"""
    try:
        os.environ["MAWARED_TOKEN"] = token
        if os.path.exists(ENV_FILE):
            set_key(ENV_FILE, "MAWARED_TOKEN", token)
    except Exception as e:
        log(f"⚠️ فشل تحديث متغيرات البيئة للتوكن: {str(e)}")

def safe_load_token() -> str:
    """تحميل التوكن من مصادر متعددة"""
    global _token_cache

    current_time = time.time()
    if (_token_cache["token"] and
        current_time - _token_cache["timestamp"] < _token_cache["ttl"]):
        return _token_cache["token"]

    token = ""

    # 1) من متغير البيئة
    env_token = os.environ.get("MAWARED_TOKEN", "").strip()
    if env_token:
        token = env_token

    # 2) من ملف .env
    if not token and os.path.exists(ENV_FILE):
        try:
            env_vals = dotenv_values(ENV_FILE)
            file_token = env_vals.get("MAWARED_TOKEN", "").strip()
            if file_token:
                os.environ["MAWARED_TOKEN"] = file_token
                token = file_token
        except Exception:
            pass

    # 3) من ملف token.txt
    if not token and os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                enc = f.read().strip()
                if enc:
                    tok = decrypt_token(enc)
                    if tok and len(tok) > 10:
                        write_env_token(tok)
                        token = tok
        except Exception:
            pass

    # 4) من ملف النسخة الاحتياطية
    if not token and os.path.exists(TOKEN_BACKUP_FILE):
        try:
            with open(TOKEN_BACKUP_FILE, "r", encoding="utf-8") as f:
                enc = f.read().strip()
                if enc:
                    tok = decrypt_token(enc)
                    if tok and len(tok) > 10:
                        write_env_token(tok)
                        try:
                            with open(TOKEN_FILE, "w", encoding="utf-8") as tf:
                                tf.write(encrypt_token(tok))
                        except Exception:
                            pass
                        token = tok
        except Exception:
            pass

    if not token:
        log("❌ لم يتم العثور على توكن صالح في أي مصدر")
    else:
        _token_cache["token"] = token
        _token_cache["timestamp"] = current_time
        _token_cache["ttl"] = 3600

    return token

def save_token(token: str):
    """حفظ التوكن في جميع المصادر"""
    if not token or len(token) < 10:
        log("⚠️ محاولة حفظ توكن غير صالح")
        return

    clear_token_cache()
    write_env_token(token)
    enc = encrypt_token(token)

    global _token_cache
    _token_cache["token"] = token
    _token_cache["timestamp"] = time.time()
    _token_cache["ttl"] = 3600

    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(enc)
        log("✅ تم حفظ التوكن في token.txt")
    except Exception as e:
        log(f"⚠️ فشل حفظ token.txt: {str(e)}")

    try:
        with open(TOKEN_BACKUP_FILE, "w", encoding="utf-8") as f:
            f.write(enc)
        log("✅ تم حفظ النسخة الاحتياطية للتوكن")
    except Exception as e:
        log(f"⚠️ فشل حفظ token_backup.txt: {str(e)}")

# ------------------------------
# رؤوس طلبات Mawared API
# ------------------------------
def api_headers():
    """إرجاع رؤوس HTTP للطلبات"""
    token = safe_load_token()
    if not token:
        log("⚠️ api_headers: التوكن فارغ أو مفقود")
        # إرجاع headers بدون توكن (سيسبب أخطاء 401 من API)
        token = ""

    headers = {
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {token}",
        "Apicode": API_CODE,
        "Appversion": APP_VERSION,
        "Platform": PLATFORM,
        "Accept-Language": "ar-SA",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Connection": "keep-alive",
        "X-Device-Type": "iPhone",
        "X-OS-Version": "17.0.0",
        "X-App-Build": "1"
    }
    return headers

# ------------------------------
# الحصول على وقت النظام من خادم موارد
# ------------------------------
def get_system_time(info=None):
    """الحصول على الوقت من خادم موارد أو استخدام الوقت المحلي"""
    try:
        if info is None:
            info = ensure_info()
        if not info or not isinstance(info, dict):
            raise ValueError("معلومات الموظف غير متوفرة")

        employee_id = info.get("employeeID")
        if not employee_id:
            raise ValueError("employeeID غير موجود في معلومات الموظف")

        url = f"https://mawaredapi.moh.gov.sa/WebAPI217/Employee/{employee_id}/AttendanceManagement/Geolocations/Validate"
        resp = requests.post(url, headers=api_headers(), timeout=15)
        
        if resp.status_code == 200:
            data = resp.json()
            system_time = data.get("systemTime")
            if system_time:
                if "T" in system_time:
                    log(f"✅ SYSTEM TIME (from server): {system_time}")
                    return system_time
                else:
                    try:
                        dt = datetime.strptime(system_time, "%Y-%m-%d %H:%M:%S")
                        formatted_time = dt.strftime("%Y-%m-%dT%H:%M:%S")
                        log(f"✅ SYSTEM TIME (converted): {formatted_time}")
                        return formatted_time
                    except Exception:
                        log(f"⚠️ تنسيق وقت غير معروف: {system_time}")
                        return system_time
            else:
                log("⚠️ لم يتم العثور على systemTime في الاستجابة")
        else:
            safe_log_response(resp.text, f"⚠️ get_system_time: status={resp.status_code}, body=")
    except Exception as e:
        log(f"⚠️ get_system_time exception: {str(e)}")

    # استخدام الوقت المحلي كبديل
    now = datetime.now(KSA_TZ)
    fallback = now.strftime("%Y-%m-%dT%H:%M:%S")
    log(f"⚠️ SYSTEM TIME FALLBACK (local KSA): {fallback}")
    return fallback

# ------------------------------
# تهيئة بيانات الموظف
# ------------------------------
def init_employee():
    """جلب بيانات الموظف من API"""
    log("🔍 INIT_EMPLOYEE: تم البدء في تهيئة بيانات الموظف من واجهة Mawared API")

    try:
        # 1) الحصول على معلومات المستخدم
        resp = requests.get(
            "https://mawaredauth.moh.gov.sa/AuthorizationServer217/connect/userinfo",
            headers=api_headers(),
            timeout=15,
        )
        
        if resp.status_code != 200:
            safe_log_response(resp.text, f"❌ INIT_EMPLOYEE userinfo status={resp.status_code}, body=")
            return False

        info_json = resp.json()
        employee_id = info_json.get("EmployeeNumber") or info_json.get("employeeNumber") or info_json.get("employeeID")
        
        if not employee_id:
            log("❌ INIT_EMPLOYEE: EmployeeNumber غير موجود في userinfo")
            return False

        log(f"✅ INIT_EMPLOYEE: EmployeeNumber={employee_id}")

        # 2) الحصول على مواقع الموظف
        geo_url = f"https://mawaredapi.moh.gov.sa/WebAPI217/Employee/{employee_id}/AttendanceManagement/Geolocations"
        params = {
            "targetEmployeeNumber": employee_id,
            "employeeNumber": employee_id,
        }

        resp2 = requests.get(
            geo_url,
            headers=api_headers(),
            params=params,
            timeout=15,
        )

        if resp2.status_code != 200:
            safe_log_response(resp2.text, f"❌ INIT_EMPLOYEE Geolocations status={resp2.status_code}, body=")
            return False

        locs = resp2.json()

        # معالجة الرد
        if isinstance(locs, dict):
            # التحقق من وجود أخطاء
            error_keys = ["error", "Error", "message", "Message", "error_description"]
            for key in error_keys:
                if key in locs:
                    error_msg = locs.get(key)
                    log(f"❌ INIT_EMPLOYEE: Mawared API returned error: {key}={error_msg}")
                    return False

            # البحث عن القائمة في الهيكل
            list_keys = ["items", "data", "results", "list", "geolocations", "locations"]
            for key in list_keys:
                if key in locs and isinstance(locs[key], list):
                    locs = locs[key]
                    log(f"✅ تم العثور على القائمة في المفتاح '{key}'")
                    break
            else:
                log(f"❌ INIT_EMPLOYEE: هيكل JSON غير متوقع. المفاتيح: {list(locs.keys())}")
                return False

        if not isinstance(locs, list) or not locs:
            log("❌ INIT_EMPLOYEE: قائمة Geolocations فارغة أو غير صالحة")
            return False

        first_loc = locs[0]
        log(f"📌 أول موقع: {first_loc}")

        # استخراج locationId
        location_id = None
        possible_keys = ["locationId", "id", "LocationId", "location_id", "locationID"]

        for key in possible_keys:
            if key in first_loc:
                location_id = first_loc.get(key)
                log(f"✅ تم العثور على locationId في المفتاح '{key}': {location_id}")
                break

        if not location_id:
            log(f"❌ INIT_EMPLOYEE: لم يتم العثور على locationId في Geolocations")
            return False

        # حفظ البيانات
        info_data = {
            "employeeID": employee_id,
            "employeeNumber": employee_id,
            "locationId": location_id,
            "raw_userinfo": info_json,
            "raw_first_location": first_loc,
            "last_updated": datetime.now(KSA_TZ).isoformat(),
        }
        save_json(INFO_FILE, info_data)
        log(f"✅ INIT_EMPLOYEE: تم حفظ بيانات الموظف: employeeID={employee_id}, locationId={location_id}")
        return True

    except Exception as e:
        log(f"❌ INIT_EMPLOYEE exception: {str(e)}")
        return False

def ensure_info():
    """التأكد من أن معلومات الموظف متاحة وحديثة"""
    info = load_json(INFO_FILE, default=None)
    
    if info and isinstance(info, dict):
        last_updated_str = info.get("last_updated")
        if last_updated_str:
            try:
                last_updated = datetime.fromisoformat(last_updated_str)
                if datetime.now(KSA_TZ) - last_updated > timedelta(hours=24):
                    log("ℹ️ معلومات الموظف قديمة - إعادة التهيئة")
                    if init_employee():
                        return load_json(INFO_FILE, default=None)
                    return None
                return info
            except Exception as e:
                log(f"⚠️ ensure_info parsing last_updated: {str(e)}")
        else:
            log("ℹ️ لا يوجد last_updated - إعادة التهيئة")

    # محاولة التهيئة
    for attempt in range(3):
        log(f"🔁 ensure_info: محاولة تهيئة رقم {attempt + 1}")
        if init_employee():
            info2 = load_json(INFO_FILE, default=None)
            if info2:
                return info2
        time.sleep(3)

    log("❌ ensure_info: فشل جميع محاولات التهيئة")
    return None

# ------------------------------
# دوال الحضور / الانصراف / السجل
# ------------------------------
def perform_attendance(action: str):
    """تنفيذ حضور/انصراف"""
    info = ensure_info()
    if not info:
        return False, "فشل تهيئة بيانات الموظف - تأكد من صحة التوكن"

    employee_id = info.get("employeeID")
    employee_number = info.get("employeeNumber") or employee_id
    location_id = info.get("locationId")

    if not employee_id or not location_id:
        return False, "بيانات الموظف أو الموقع غير مكتملة"

    action_time = get_system_time(info)
    if not action_time:
        action_time = datetime.now(KSA_TZ).strftime("%Y-%m-%dT%H:%M:%S")
        log(f"⚠️ استخدام الوقت المحلي: {action_time}")

    url = f"https://mawaredapi.moh.gov.sa/WebAPI217/Employee/{employee_id}/AttendanceManagement/Geolocations/{action}"
    params = {
        "actionTime": action_time,
        "targetEmployeeNumber": employee_number,
        "locationId": location_id,
    }

    action_name = "تسجيل الدخول" if action == "checkin" else "تسجيل الخروج"

    try:
        log(f"📤 {action_name}: إرسال طلب إلى {url}")
        log(f"📤 المعطيات: {params}")

        resp = requests.post(url, headers=api_headers(), params=params, timeout=20)
        log(f"📥 {action_name}: status={resp.status_code}")
        
        # تسجيل الرد بشكل آمن
        response_text = resp.text
        safe_log_response(response_text[:500], f"📥 {action_name} body: ")

        # محاولة تحليل JSON
        try:
            data = resp.json()
        except:
            data = {"raw": response_text, "status_code": resp.status_code}

        # تحليل النتيجة
        ok = resp.status_code == 200

        if "state" in data and data["state"]:
            message = data["state"]
            error_keywords = ["لا يمكن", "خطأ", "Error", "ORA-", "غير مسموح", "فشل", "غير صالح", "لايوجد", "غير مصرح"]
            if any(err in message for err in error_keywords):
                ok = False
            else:
                ok = True
        elif ok:
            message = "تم التسجيل بنجاح"
        else:
            message = "حدث خطأ غير محدد أثناء المعالجة"
            if "Message" in data:
                message = data.get("Message", message)
            elif "message" in data:
                message = data.get("message", message)
            elif resp.status_code == 401:
                message = "خطأ في التوكن (401 Unauthorized)"
            elif resp.status_code == 403:
                message = "غير مصرح (403 Forbidden)"

        if ok:
            return True, f"{action_name}: {message}"
        else:
            return False, f"{action_name}: {message}"

    except requests.exceptions.Timeout:
        log(f"❌ {action_name}: انتهت مهلة الاتصال")
        return False, f"{action_name} فشل: انتهت مهلة الاتصال"
    except requests.exceptions.ConnectionError:
        log(f"❌ {action_name}: خطأ في الاتصال")
        return False, f"{action_name} فشل: خطأ في الاتصال"
    except Exception as e:
        log(f"❌ {action_name} exception: {str(e)}")
        return False, f"{action_name} فشل: خطأ في الاتصال"

def perform_history():
    """جلب سجل الحضور"""
    info = ensure_info()
    if not info:
        return False, "فشل تهيئة بيانات الموظف - تأكد من صحة التوكن"

    employee_id = info.get("employeeID")
    employee_number = info.get("employeeNumber") or employee_id

    if not employee_id:
        return False, "employeeID غير موجود"

    date_str = datetime.now(KSA_TZ).strftime("%Y/%m/%d")
    url = f"https://mawaredapi.moh.gov.sa/WebAPI217/Employee/{employee_id}/AttendanceManagement/Transactions"
    params = {"date": date_str, "employeeNumber": employee_number}

    try:
        resp = requests.get(url, headers=api_headers(), params=params, timeout=20)

        if resp.status_code == 200:
            data = resp.json()
            simplified_data = []
            
            if isinstance(data, list):
                for transaction in data:
                    transaction_time = transaction.get("transactionTime", "")
                    transaction_type = transaction.get("transactionType", "")

                    if "دخول" in transaction_type or "In" in transaction_type or "Checkin" in transaction_type:
                        simplified_type = "دخول"
                    elif "خروج" in transaction_type or "Out" in transaction_type or "Checkout" in transaction_type:
                        simplified_type = "خروج"
                    else:
                        simplified_type = transaction_type

                    simplified_data.append({
                        "transactionTime": transaction_time,
                        "transactionType": simplified_type
                    })

            return True, simplified_data

        safe_log_response(resp.text, f"فشل جلب السجل: ")
        return False, f"فشل جلب السجل: {resp.status_code}"

    except Exception as e:
        log(f"❌ HISTORY exception: {str(e)}")
        return False, f"فشل جلب السجل: {str(e)}"

# ------------------------------
# إدارة ملف auto.json
# ------------------------------
def load_auto():
    """تحميل إعدادات الحضور الآلي"""
    default = {
        "enabled": False,
        "checkin": {"start": AUTO_CHECKIN_START, "end": AUTO_CHECKIN_END},
        "checkout": {"start": AUTO_CHECKOUT_START, "end": AUTO_CHECKOUT_END},
    }
    cfg = load_json(AUTO_FILE, default=default)
    cfg.setdefault("enabled", False)
    cfg.setdefault("checkin", {"start": AUTO_CHECKIN_START, "end": AUTO_CHECKIN_END})
    cfg.setdefault("checkout", {"start": AUTO_CHECKOUT_START, "end": AUTO_CHECKOUT_END})
    return cfg

def save_auto(cfg):
    """حفظ إعدادات الحضور الآلي"""
    save_json(AUTO_FILE, cfg)

# ------------------------------
# أوقات عشوائية + منطق التحقق الذكي
# ------------------------------
def random_time_between(start_str, end_str):
    """إرجاع وقت عشوائي بين start و end"""
    fmt = "%H:%M"
    try:
        t1 = datetime.strptime(start_str, fmt)
        t2 = datetime.strptime(end_str, fmt)
    except Exception as e:
        log(f"⚠️ random_time_between parsing error: {str(e)}")
        t1 = datetime.strptime("07:00", fmt)
        t2 = datetime.strptime("07:30", fmt)

    if t2 <= t1:
        log(f"⚠️ تميّزت الفترة الزمنية بخلل في الترتيب: {start_str} - {end_str}")
        t2 = t1 + timedelta(minutes=30)

    delta_minutes = int((t2 - t1).total_seconds() // 60)
    if delta_minutes <= 0:
        delta_minutes = 30
    
    rnd = random.randint(0, delta_minutes)
    final = (t1 + timedelta(minutes=rnd)).strftime("%H:%M")
    return final

def is_time_in_range(current_time_str, start_str, end_str):
    """التحقق إذا كان الوقت الحالي ضمن النطاق المحدد"""
    try:
        current = datetime.strptime(current_time_str, "%H:%M")
        start = datetime.strptime(start_str, "%H:%M")
        end = datetime.strptime(end_str, "%H:%M")

        if start <= end:
            return start <= current <= end
        else:
            return current >= start or current <= end
    except Exception as e:
        log(f"⚠️ خطأ في التحقق من النطاق الزمني: {str(e)}")
        return False

# ------------------------------
# توليد الأوقات اليومية في الساعة 7 صباحاً
# ------------------------------
def generate_daily_times_at_7am():
    """توليد أوقات الدخول والخروج اليومية"""
    try:
        cfg = load_auto()
        if not cfg.get("enabled", False):
            log("⏸️ GENERATE_TIMES_7AM: النظام الآلي معطل - تخطي")
            return
        
        today_str = datetime.now(KSA_TZ).strftime("%Y-%m-%d")
        weekday = datetime.now(KSA_TZ).weekday()
        
        # منع التوليد في الجمعة والسبت
        if weekday in (4, 5):
            log(f"⛔ GENERATE_TIMES_7AM: اليوم {['الإثنين','الثلاثاء','الأربعاء','الخميس','الجمعة','السبت','الأحد'][weekday]} - لا يتم توليد أوقات")
            return
        
        with auto_state_lock:
            if auto_state["date"] != today_str or auto_state["checkin_time"] is None:
                in_start = cfg.get("checkin", {}).get("start", AUTO_CHECKIN_START)
                in_end = cfg.get("checkin", {}).get("end", AUTO_CHECKIN_END)
                out_start = cfg.get("checkout", {}).get("start", AUTO_CHECKOUT_START)
                out_end = cfg.get("checkout", {}).get("end", AUTO_CHECKOUT_END)
                
                # تصحيح الأوقات
                try:
                    in_start_dt = datetime.strptime(in_start, "%H:%M")
                    in_end_dt = datetime.strptime(in_end, "%H:%M")
                    if in_end_dt <= in_start_dt:
                        in_end = (datetime.strptime(in_start, "%H:%M") + timedelta(minutes=30)).strftime("%H:%M")
                except:
                    pass
                    
                try:
                    out_start_dt = datetime.strptime(out_start, "%H:%M")
                    out_end_dt = datetime.strptime(out_end, "%H:%M")
                    if out_end_dt <= out_start_dt:
                        out_end = (datetime.strptime(out_start, "%H:%M") + timedelta(minutes=30)).strftime("%H:%M")
                except:
                    pass
                
                in_time = random_time_between(in_start, in_end)
                out_time = random_time_between(out_start, out_end)
                
                auto_state.update({
                    "date": today_str,
                    "checkin_time": in_time,
                    "checkout_time": out_time,
                    "done_in": False,
                    "done_out": False,
                })
                
                msg = (
                    f"📅 تم توليد أوقات اليوم ({today_str}) في الساعة 7 صباحاً:\n"
                    f"🟢 دخول عشوائي بين {in_start} - {in_end} → عند {in_time}\n"
                    f"🔴 خروج عشوائي بين {out_start} - {out_end} → عند {out_time}"
                )
                log(f"✅ GENERATE_TIMES_7AM: {msg}")
                
                # إرسال إشعار تليجرام فقط إذا كان النظام مفعلاً وليس عطلة
                if weekday not in (4, 5):
                    telegram(msg)
    
    except Exception as e:
        log(f"⚠️ GENERATE_TIMES_7AM exception: {str(e)}")

def auto_check_job():
    """الدالة الأساسية للحضور الآلي"""
    try:
        cfg = load_auto()
        if not cfg.get("enabled", False):
            log("⏸️ AUTO_JOB: النظام الآلي معطل - تخطي")
            return
        
        now = datetime.now(KSA_TZ)
        weekday = now.weekday()
        today_str = now.strftime("%Y-%m-%d")
        now_hm = now.strftime("%H:%M")
        
        # التحقق من العطلة
        if weekday in (4, 5):
            day_name = ['الإثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت', 'الأحد'][weekday]
            log_msg = f"⛔ AUTO_JOB: تم منع الحضور الآلي اليوم لأنه {day_name} (يوم عطلة)"
            
            if not holiday_already_notified(today_str):
                log(log_msg)
                mark_holiday_notified(today_str)
            else:
                log(f"⏸️ AUTO_JOB: إشعار العطلة أُرسل مسبقاً لليوم {today_str}")
            
            # تحديث الحالة للعطلة
            with auto_state_lock:
                if auto_state.get("date") != today_str:
                    auto_state.update({
                        "date": today_str,
                        "checkin_time": None,
                        "checkout_time": None,
                        "done_in": True,
                        "done_out": True,
                    })
            return
        
        with auto_state_lock:
            # إذا لم تكن الأوقات مولدة
            if auto_state["date"] != today_str or auto_state["checkin_time"] is None:
                in_start = cfg.get("checkin", {}).get("start", AUTO_CHECKIN_START)
                in_end = cfg.get("checkin", {}).get("end", AUTO_CHECKIN_END)
                out_start = cfg.get("checkout", {}).get("start", AUTO_CHECKOUT_START)
                out_end = cfg.get("checkout", {}).get("end", AUTO_CHECKOUT_END)
                
                # تصحيح الأوقات
                try:
                    in_start_dt = datetime.strptime(in_start, "%H:%M")
                    in_end_dt = datetime.strptime(in_end, "%H:%M")
                    if in_end_dt <= in_start_dt:
                        in_end = (datetime.strptime(in_start, "%H:%M") + timedelta(minutes=30)).strftime("%H:%M")
                except:
                    pass
                    
                try:
                    out_start_dt = datetime.strptime(out_start, "%H:%M")
                    out_end_dt = datetime.strptime(out_end, "%H:%M")
                    if out_end_dt <= out_start_dt:
                        out_end = (datetime.strptime(out_start, "%H:%M") + timedelta(minutes=30)).strftime("%H:%M")
                except:
                    pass
                
                in_time = random_time_between(in_start, in_end)
                out_time = random_time_between(out_start, out_end)
                
                auto_state.update({
                    "date": today_str,
                    "checkin_time": in_time,
                    "checkout_time": out_time,
                    "done_in": False,
                    "done_out": False,
                })
                
                msg = (
                    f"📅 تم توليد أوقات اليوم ({today_str}):\n"
                    f"🟢 دخول عشوائي بين {in_start} - {in_end} → عند {in_time}\n"
                    f"🔴 خروج عشوائي بين {out_start} - {out_end} → عند {out_time}"
                )
                log(f"✅ AUTO_JOB: {msg}")
                if weekday not in (4, 5):
                    telegram(msg)
            
            checkin_time = auto_state["checkin_time"]
            checkout_time = auto_state["checkout_time"]
            done_in = auto_state["done_in"]
            done_out = auto_state["done_out"]
        
        # 1) الحضور الآلي
        if not done_in and checkin_time is not None and now_hm >= checkin_time:
            if not is_time_in_range(now_hm, AUTO_CHECKIN_START, AUTO_CHECKIN_END):
                msg = (
                    f"⛔ تم حجب تنفيذ عملية تسجيل الدخول الآلي لأن الوقت الحالي {now_hm} "
                    f"خارج وقت الدخول المسموح ({AUTO_CHECKIN_START} - {AUTO_CHECKIN_END})"
                )
                log(msg)
                if weekday not in (4, 5):
                    telegram(msg)
                with auto_state_lock:
                    auto_state["done_in"] = True
            else:
                log("🟢 AUTO_JOB: بدء تسجيل الدخول الآلي...")
                ok, message = perform_attendance("checkin")
                with auto_state_lock:
                    if ok:
                        auto_state["done_in"] = True
                status_msg = f"🟢 الدخول الآلي - {message}"
                log(f"✅ AUTO_JOB: نتيجة الدخول الآلي: {message}")
                if ok and weekday not in (4, 5):
                    telegram(status_msg)
        
        # 2) الانصراف الآلي
        if not done_out and checkout_time is not None and now_hm >= checkout_time:
            if not is_time_in_range(now_hm, AUTO_CHECKOUT_START, AUTO_CHECKOUT_END):
                msg = (
                    f"⛔ تم منع تسجيل الخروج الآلي لأن الوقت الحالي {now_hm} "
                    f"خارج وقت الخروج المسموح ({AUTO_CHECKOUT_START} - {AUTO_CHECKOUT_END})"
                )
                log(msg)
                if weekday not in (4, 5):
                    telegram(msg)
                with auto_state_lock:
                    auto_state["done_out"] = True
            else:
                log("🔴 AUTO_JOB: بدء تسجيل الخروج الآلي...")
                ok, message = perform_attendance("checkout")
                with auto_state_lock:
                    if ok:
                        auto_state["done_out"] = True
                status_msg = f"🔴 الخروج الآلي - {message}"
                log(f"✅ AUTO_JOB: نتيجة الخروج الآلي: {message}")
                if ok and weekday not in (4, 5):
                    telegram(status_msg)
    
    except Exception as e:
        log(f"⚠️ AUTO_JOB exception: {str(e)}")

# ------------------------------
# ROUTES
# ------------------------------
@app.route("/")
def index():
    """الصفحة الرئيسية"""
    try:
        return send_from_directory(".", "index.html")
    except Exception as e:
        log(f"⚠️ index.html غير موجود أو خطأ في التحميل: {str(e)}")
        return """
        <html dir='rtl'>
        <head><title>Mawared Control Panel</title></head>
        <body>
        <h1>لوحة تحكم موارد</h1>
        <p>لم يتم العثور على ملف index.html.</p>
        </body>
        </html>
        """

@app.route("/status")
def route_status():
    """حالة عامة للنظام"""
    cfg = load_auto()
    return jsonify({
        "auto_mode": cfg.get("enabled", False),
        "timestamp": datetime.now(KSA_TZ).isoformat(),
        "auto_state": auto_state,
    })

@app.route("/config")
def get_config():
    """إعدادات عامة للتطبيق"""
    return jsonify({
        "show_token_management": SHOW_TOKEN_MANAGEMENT,
        "telegram_configured": bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
        "app_version": APP_VERSION,
        "platform": PLATFORM,
    })

@app.route("/updateToken", methods=["POST"])
def update_token():
    """استقبال توكن جديد"""
    data = request.get_json() or {}
    new_token = data.get("token", "").strip()
    if not new_token:
        return jsonify({"ok": False, "message": "التوكن مفقود"})

    save_token(new_token)

    if init_employee():
        telegram("✅ تم تحديث التوكن وتهيئة بيانات الموظف بنجاح")
        return jsonify({"ok": True, "message": "تم تحديث التوكن وتهيئة بيانات الموظف بنجاح"})
    else:
        telegram("⚠️ تم تحديث التوكن لكن فشلت تهيئة بيانات الموظف")
        return jsonify({"ok": False, "message": "تم تحديث التوكن لكن فشلت تهيئة بيانات الموظف"})

@app.route("/check", methods=["POST"])
def route_check():
    """تسجيل الدخول اليدوي"""
    if not can_make_request("check"):
        return jsonify({"ok": False, "message": "⏳ انتظر قليلاً قبل محاولة أخرى"})

    today_str = datetime.now(KSA_TZ).strftime("%Y-%m-%d")
    with auto_state_lock:
        if auto_state["date"] == today_str and auto_state["done_in"]:
            log("⚠️ تم تسجيل دخول اليوم بالفعل (آلياً)")

    ok, message = perform_attendance("checkin")

    if ok:
        log("✅ تم تسجيل دخول يدوي بنجاح")

    status_msg = f"🟢 الدخول اليدوي - {message}"
    telegram(status_msg)
    return jsonify({"ok": ok, "message": message})

@app.route("/checkout", methods=["POST"])
def route_checkout():
    """تسجيل الخروج اليدوي"""
    if not can_make_request("checkout"):
        return jsonify({"ok": False, "message": "⏳ انتظر قليلاً قبل محاولة أخرى"})

    today_str = datetime.now(KSA_TZ).strftime("%Y-%m-%d")
    with auto_state_lock:
        if auto_state["date"] == today_str and auto_state["done_out"]:
            log("⚠️ تم تسجيل خروج اليوم بالفعل (آلياً)")

    ok, message = perform_attendance("checkout")

    if ok:
        log("✅ تم تسجيل خروج يدوي بنجاح")

    status_msg = f"🔴 الخروج اليدوي - {message}"
    telegram(status_msg)
    return jsonify({"ok": ok, "message": message})

@app.route("/history", methods=["POST"])
def route_history():
    """جلب سجل الحضور"""
    ok, data = perform_history()
    if ok:
        return jsonify({"ok": True, "records": data})
    return jsonify({"ok": False, "message": data})

@app.route("/autoon", methods=["POST"])
def route_autoon():
    """تشغيل الحضور الآلي"""
    cfg = load_auto()
    cfg["enabled"] = True
    save_auto(cfg)

    with auto_state_lock:
        auto_state["date"] = None

    now = datetime.now(KSA_TZ)
    weekday = now.weekday()
    
    if weekday in (4, 5):
        day_name = ['الإثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت', 'الأحد'][weekday]
        log_msg = f"⚠️ تم تشغيل النظام الآلي لكن اليوم {day_name} (يوم عطلة)"
        log(log_msg)
    else:
        telegram("🚀 تم تشغيل الحضور الآلي (Cloud Run Edition)")
    
    log("✅ تم تشغيل النظام الآلي")
    
    return jsonify({
        "ok": True,
        "message": "تم تشغيل الحضور الآلي. تأكد من إعداد Cloud Scheduler لاستدعاء /force-auto-check كل 5 دقائق."
    })

@app.route("/autooff", methods=["POST"])
def route_autooff():
    """إيقاف الحضور الآلي"""
    cfg = load_auto()
    cfg["enabled"] = False
    save_auto(cfg)
    
    now = datetime.now(KSA_TZ)
    weekday = now.weekday()
    
    if weekday not in (4, 5):
        telegram("⏸️ تم إيقاف الحضور الآلي")
    
    log("⏸️ تم إيقاف النظام الآلي")
    return jsonify({"ok": True, "message": "تم إيقاف الحضور الآلي."})

@app.route("/schedule", methods=["GET"])
def route_schedule():
    """الحصول على إعدادات الجدولة"""
    cfg = load_auto()

    scheduler_running = cfg.get("enabled", False)
    now = datetime.now(KSA_TZ)
    today_str = now.strftime("%Y-%m-%d")
    weekday = now.weekday()

    auto_status = "stopped"
    if cfg.get("enabled", False):
        if weekday in (4, 5):
            auto_status = "holiday_stopped"
        else:
            now_hm = now.strftime("%H:%M")
            if AUTO_CHECKIN_START <= now_hm <= AUTO_CHECKIN_END:
                auto_status = "in_checkin_window"
            elif AUTO_CHECKOUT_START <= now_hm <= AUTO_CHECKOUT_END:
                auto_status = "in_checkout_window"
            else:
                auto_status = "outside_work_hours"

    return jsonify({
        "enabled": cfg.get("enabled", False),
        "checkin": cfg.get("checkin", {}),
        "checkout": cfg.get("checkout", {}),
        "today": auto_state,
        "scheduler_running": scheduler_running,
        "scheduler_paused": False,
        "auto_status": auto_status,
        "auto_checkin_window": f"{AUTO_CHECKIN_START} - {AUTO_CHECKIN_END}",
        "auto_checkout_window": f"{AUTO_CHECKOUT_START} - {AUTO_CHECKOUT_END}",
        "current_time": now.strftime("%H:%M"),
        "current_day": ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"][weekday]
    })

@app.route("/health")
def health_check():
    """فحص صحة النظام"""
    token = safe_load_token()
    token_status = "exists" if token else "missing"
    cfg = load_auto()

    now = datetime.now(KSA_TZ)
    now_hm = now.strftime("%H:%M")
    weekday = now.weekday()

    in_allowed_time = False
    if weekday not in (4, 5):
        if (AUTO_CHECKIN_START <= now_hm <= AUTO_CHECKIN_END) or \
           (AUTO_CHECKOUT_START <= now_hm <= AUTO_CHECKOUT_END):
            in_allowed_time = True

    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now(KSA_TZ).isoformat(),
        "auto_enabled": cfg.get("enabled", False),
        "platform_env": "render" if is_render() else "other",
        "show_token_management": SHOW_TOKEN_MANAGEMENT,
        "token_status": token_status,
        "token_length": len(token) if token else 0,
        "app_version": APP_VERSION,
        "platform": PLATFORM,
        "scheduler_running": cfg.get("enabled", False),
        "scheduler_paused": False,
        "time_protection": {
            "enabled": True,
            "checkin_window": f"{AUTO_CHECKIN_START}-{AUTO_CHECKIN_END}",
            "checkout_window": f"{AUTO_CHECKOUT_START}-{AUTO_CHECKOUT_END}",
            "current_time": now_hm,
            "in_allowed_time": in_allowed_time,
            "is_weekend": weekday in (4, 5),
            "day_name": ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"][weekday]
        }
    })

@app.route("/force-auto-check", methods=["GET", "POST"])
def force_auto_check():
    """استدعاء يدوي/آلي للحضور الآلي"""
    log("🔧 FORCE_AUTO_CHECK: بدء التحقق الآلي عبر /force-auto-check")

    try:
        auto_check_job()
        return jsonify({"ok": True, "message": "تم تنفيذ الحضور الآلي (إن وجد شيء للتنفيذ)."})
    except Exception as e:
        log(f"❌ FORCE_AUTO_CHECK exception: {str(e)}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/generate-daily-times", methods=["GET", "POST"])
def generate_daily_times():
    """توليد الأوقات اليومية"""
    try:
        log("⏰ GENERATE_DAILY_TIMES: بدء توليد الأوقات اليومية...")
        generate_daily_times_at_7am()
        return jsonify({"ok": True, "message": "تم محاولة توليد الأوقات اليومية"})
    except Exception as e:
        log(f"❌ GENERATE_DAILY_TIMES exception: {str(e)}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/reset-auto-state", methods=["POST"])
def reset_auto_state():
    """إعادة تعيين حالة النظام الآلي"""
    try:
        with auto_state_lock:
            today_str = datetime.now(KSA_TZ).strftime("%Y-%m-%d")
            auto_state.update({
                "date": today_str,
                "checkin_time": None,
                "checkout_time": None,
                "done_in": False,
                "done_out": False,
            })
        
        log(f"🔄 RESET_AUTO_STATE: تم إعادة تعيين الحالة للتاريخ {today_str}")
        
        # توليد أوقات جديدة
        cfg = load_auto()
        if cfg.get("enabled", False):
            in_start = cfg.get("checkin", {}).get("start", AUTO_CHECKIN_START)
            in_end = cfg.get("checkin", {}).get("end", AUTO_CHECKIN_END)
            out_start = cfg.get("checkout", {}).get("start", AUTO_CHECKOUT_START)
            out_end = cfg.get("checkout", {}).get("end", AUTO_CHECKOUT_END)
            
            with auto_state_lock:
                in_time = random_time_between(in_start, in_end)
                out_time = random_time_between(out_start, out_end)
                auto_state["checkin_time"] = in_time
                auto_state["checkout_time"] = out_time
            
            msg = (
                f"🔄 تم إعادة تعيين وتوليد أوقات جديدة:\n"
                f"🟢 دخول عند: {in_time}\n"
                f"🔴 خروج عند: {out_time}"
            )
            log(f"✅ {msg}")
            
            weekday = datetime.now(KSA_TZ).weekday()
            if weekday not in (4, 5):
                telegram(msg)
        
        return jsonify({"ok": True, "message": "تم إعادة تعيين حالة النظام الآلي وتوليد أوقات جديدة"})
    
    except Exception as e:
        log(f"❌ RESET_AUTO_STATE exception: {str(e)}")
        return jsonify({"ok": False, "message": f"خطأ في إعادة التعيين: {str(e)}"})

@app.route("/force-init", methods=["GET", "POST"])
def route_force_init():
    """إعادة تهيئة بيانات الموظف"""
    if init_employee():
        telegram("✅ تمت إعادة تهيئة بيانات الموظف بنجاح")
        return jsonify({"ok": True, "message": "تمت إعادة التهيئة بنجاح"})
    telegram("⚠️ فشل إعادة تهيئة بيانات الموظف")
    return jsonify({"ok": False, "message": "تعذر تنفيذ عملية إعادة التهيئة"})

@app.route("/employee-info", methods=["GET"])
def employee_info():
    """بيانات الموظف"""
    info = load_json(INFO_FILE, default=None)

    if not info or not isinstance(info, dict):
        return jsonify({"ok": False, "has_data": False, "message": "لا توجد بيانات للموظف"})

    return jsonify({
        "ok": True,
        "has_data": True,
        "employeeID": info.get("employeeID"),
        "employeeNumber": info.get("employeeNumber"),
        "locationId": info.get("locationId"),
        "last_updated": info.get("last_updated"),
    })

# ... باقي routes تبقى كما هي ...

# ------------------------------
# تشغيل التطبيق
# ------------------------------
if __name__ == "__main__":
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        log(f"✅ مجلد البيانات: {APP_DIR}")
    except Exception as e:
        log(f"⚠️ لا يمكن إنشاء مجلد البيانات: {str(e)}")

    log(f"✅ بدء تشغيل MAWARED PYTHON PRO – الإصدار {APP_VERSION}")
    log(f"🔧 Platform={PLATFORM}, ApiCode={API_CODE}")
    log(f"🔧 User-Agent={USER_AGENT}")
    log(f"🔧 ملف .env موجود؟ {os.path.exists(ENV_FILE)}")

    reset_auto_state_daily()

    _tok = safe_load_token()
    if _tok:
        log("✅ تم العثور على توكن عند بدء التشغيل")
    else:
        log("⚠️ لا يوجد توكن مضبوط حالياً - تحتاج لتحديث التوكن من الواجهة")

    port = int(os.environ.get("PORT", 8080))
    log(f"🌐 التشغيل على المنفذ: {port}")

    try:
        token = safe_load_token()
        if token and len(token) > 10:
            log("🔄 AUTO-INIT: تم العثور على التوكن، بدء تهيئة بيانات الموظف...")
            init_employee()
        else:
            log("⚠️ AUTO-INIT: لا يوجد توكن، لن يتم التهيئة التلقائية")
    except Exception as e:
        log(f"❌ AUTO-INIT ERROR: {e}")

    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)



