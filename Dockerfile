# ==============================
# MAWARED CLOUD RUN EDITION
# Dockerfile (Optimized)
# ==============================

# 1) Python خفيف
FROM python:3.10-slim

# 2) تعيين مجلد العمل
WORKDIR /app

# 3) تثبيت المكتبات المطلوبة
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4) نقل المشروع بالكامل
COPY . .

# 5) إنشاء مجلد البيانات داخل الحاوية
RUN mkdir -p /app/mawared_data

# 6) المتغيرات الافتراضية (Cloud Run يكتب PORT تلقائيًا)
ENV PORT=10000
ENV PYTHONUNBUFFERED=1

# 7) تشغيل التطبيق
CMD ["python", "app.py"]
