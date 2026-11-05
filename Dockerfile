FROM node:22-alpine AS frontend-build

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FRONTEND_DIR=/app/frontend

WORKDIR /app/backend

COPY backend/requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ /app/backend/
COPY frontend/index.html /app/frontend/index.html
COPY frontend/js/ /app/frontend/js/
COPY frontend/css/styles.css /app/frontend/css/styles.css
COPY --from=frontend-build /build/frontend/css/tailwind.css /app/frontend/css/tailwind.css
RUN DJANGO_DEBUG=1 python manage.py collectstatic --noinput \
    && chmod +x /app/backend/entrypoint.sh \
    && useradd --system --create-home appuser
USER appuser

EXPOSE 8000
ENTRYPOINT ["/app/backend/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--access-logfile", "-"]
