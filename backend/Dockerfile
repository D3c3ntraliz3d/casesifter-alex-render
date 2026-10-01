FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV PYTHONUNBUFFERED=1
ENV PORT=8000
# Analysis image defaults — local John testing uses APP_MODE=vault outside Docker.
ENV APP_MODE=analysis
ENV APP_ENV=production
ENV CORS_ALLOW_ORIGINS=https://casesifter.com,https://www.casesifter.com,https://casesifter-marketing.onrender.com
EXPOSE 8000
# Render injects $PORT; default 8000 for local/compose.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
