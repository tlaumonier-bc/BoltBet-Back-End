# ===== build stage: install dependencies =====
FROM eu.gcr.io/blockchain-internal/v1/blockchain_python_3_13_build:latest AS builder

WORKDIR /app
COPY requirements.txt .
# CONFIRM: pip flags / install location expected by the base image.
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ===== runtime stage =====
FROM eu.gcr.io/blockchain-internal/v1/blockchain_python_3_13:latest

WORKDIR /app
COPY --from=builder /install /usr/local
COPY . .

# Collect static files so whitenoise can serve them.
RUN python manage.py collectstatic --noinput

# Run as the required non-root user.
USER blockchain          # CONFIRM: base image may already create/set this (uid 1000)

EXPOSE 8000
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "boltbet_backend.asgi:application"]