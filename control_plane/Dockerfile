FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Runtime modules only (main imports these at boot or on auth paths).
COPY main.py auth_store.py mailer.py rate_limit.py edge_security.py session_cookie.py oidc_auth.py totp_auth.py ./
ENV PYTHONUNBUFFERED=1
ENV APP_ENV=production
ENV PORT=8010
EXPOSE 8010
# Render injects $PORT; default 8010 for local/compose.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8010}"]
