FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FRONTEND_DIR=/app/frontend

WORKDIR /app/backend

COPY backend/requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ /app/backend/
COPY frontend/ /app/frontend/
RUN chmod +x /app/backend/entrypoint.sh \
    && useradd --system --create-home appuser
USER appuser

EXPOSE 8000
ENTRYPOINT ["/app/backend/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--access-logfile", "-"]
