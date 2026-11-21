FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ติดตั้งไลบรารีแยกเลเยอร์ก่อน เพื่อใช้ Docker layer cache ได้เต็มที่
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# คัดลอกโค้ด FastAPI และ ADK Agent เข้าสู่คอนเทนเนอร์
COPY . /app

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]