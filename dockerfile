FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run يمرر PORT عبر متغير البيئة، التطبيق يستخدمه تلقائياً
ENV PORT=8080

CMD ["python", "app.py"]
