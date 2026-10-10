FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py auth_store.py ./
ENV PYTHONUNBUFFERED=1
ENV PORT=8010
EXPOSE 8010
# Render injects $PORT; default 8010 for local/compose.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8010}"]
