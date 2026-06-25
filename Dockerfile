# ===== build stage =====
FROM python:3.13-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ===== runtime stage =====
FROM python:3.13-slim

WORKDIR /app
COPY --from=builder /install /usr/local
COPY . .

# collectstatic does NOT open a database connection. DJANGO_ENV is unset during
# the build, so the production DB_HOST guard in settings.py does not fire.
RUN python manage.py collectstatic --noinput

# Respect the platform-provided port (Cloud Run sets $PORT); default to 8000,
# which matches the port declared in the APPLICATIONS app-definition.yml.
EXPOSE 8000
CMD ["sh", "-c", "daphne -b 0.0.0.0 -p ${PORT:-8000} lightning_map_game_backend.asgi:application"]
