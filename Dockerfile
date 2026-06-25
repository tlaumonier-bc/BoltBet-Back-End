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

# collectstatic does NOT open a database connection. DJANGO_ENV is unset during
# the build, so the production DB_HOST guard in settings.py does not fire.
RUN python manage.py collectstatic --noinput

USER blockchain

# Respect the platform-provided port (Cloud Run sets $PORT); default to 8000,
# which matches the port declared in the APPLICATIONS app-definition.yml.
EXPOSE 8000
CMD ["sh", "-c", "daphne -b 0.0.0.0 -p ${PORT:-8000} lightning_map_game_backend.asgi:application"]
