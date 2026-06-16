# ===== build stage =====
FROM eu.gcr.io/blockchain-internal/v1/blockchain_python_3_13_build:latest AS builder

# These images default to the non-root "blockchain" user, which can't write to
# /install at the root. Switch to root for the install — this stage is discarded;
# only /install is copied forward.
USER root

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ===== runtime stage =====
FROM eu.gcr.io/blockchain-internal/v1/blockchain_python_3_13:latest

# Root for the build-time steps (copy deps, collectstatic), then drop to the
# non-root blockchain user for the actual run.
USER root

WORKDIR /app
COPY --from=builder /install /usr/local
COPY . .

# Needs no DB/Vault; settings fall back to SQLite when DB_HOST is unset.
RUN python manage.py collectstatic --noinput

USER blockchain

EXPOSE 8000
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "boltbet_backend.asgi:application"]