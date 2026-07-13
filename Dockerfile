FROM python:3.12-slim

# Don't write .pyc, unbuffered logs (so docker logs show prints immediately)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Writable data dir for users.json + audit.db (mounted as a volume in compose)
RUN mkdir -p /data
ENV USERS_FILE=/data/users.json \
    AUDIT_DB=/data/audit.db

EXPOSE 8000

# Shell form so ${GUNICORN_WORKERS}/${GUNICORN_THREADS} expand from env.
# timeout 600 lets large bulk-zip downloads finish.
CMD gunicorn \
    --workers ${GUNICORN_WORKERS:-3} \
    --threads ${GUNICORN_THREADS:-4} \
    --timeout 600 \
    --bind 0.0.0.0:8000 \
    app:app
