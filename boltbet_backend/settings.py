"""
Django settings for boltbet_backend project.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name, default=False):
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes", "on")


def _env_list(name, default=""):
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


# "development" (default) or "production". Controls the production-only guards
# below (e.g. requiring an explicit DB_HOST).
DJANGO_ENV = os.environ.get("DJANGO_ENV", "development").lower()
IS_PRODUCTION = DJANGO_ENV == "production"

# Insecure default kept ONLY so local dev runs without config.
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-33s(c2u98)g!jkw_osnyo=+^80#fjqn_@o*cf=57c24_@-2$h3",
)

# Defaults to False per spec. Set DJANGO_DEBUG=true in your local .env.
DEBUG = _env_bool("DJANGO_DEBUG", False)

ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

# Behind the dev/prod HTTPS proxy, trust the forwarded scheme + admin POSTs.
CSRF_TRUSTED_ORIGINS = _env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "")
if _env_bool("DJANGO_BEHIND_PROXY", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Application definition
INSTALLED_APPS = [
    'daphne',  # MUST BE FIRST FOR WEBSOCKETS!
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third-party apps
    'rest_framework',
    'corsheaders',
    'channels',

    # Local Apps
    'lightning',
    'account',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',  # MUST BE HIGH IN THE LIST
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# Narrowed from CORS_ALLOW_ALL_ORIGINS to an env-driven allowlist.
CORS_ALLOWED_ORIGINS = _env_list("CORS_ALLOWED_ORIGINS", "http://localhost:3000")

ROOT_URLCONF = 'boltbet_backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'boltbet_backend.wsgi.application'
ASGI_APPLICATION = 'boltbet_backend.asgi.application'

# Database
# PostgreSQL in EVERY environment. There is no SQLite fallback: local dev uses
# the Postgres container from docker-compose.yml (the defaults below match it),
# and every deployed environment supplies DB_* via the platform.
DB_HOST = os.environ.get("DB_HOST", "localhost")

# In production, DB_HOST must be set explicitly. We never silently start against
# the local default — that is the "silent fallback" failure mode we want to kill.
if IS_PRODUCTION and not os.environ.get("DB_HOST"):
    raise ImproperlyConfigured(
        "DB_HOST must be set when DJANGO_ENV=production. Refusing to start "
        "against the local Postgres default."
    )

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "lightning_map_game"),
        "USER": os.environ.get("DB_USER", "postgres"),
        "PASSWORD": os.environ.get("DB_PASSWORD", "postgres"),
        "HOST": DB_HOST,
        "PORT": os.environ.get("DB_PORT", "5432"),
    }
}

CONN_MAX_AGE = int(os.environ.get("DJANGO_CONN_MAX_AGE", "60"))
CONN_HEALTH_CHECKS = True

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# DRF: anonymous-friendly MVP (no SessionAuth = no CSRF enforcement on POST).
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
}

# ==========================================
# CHANNELS & REDIS CONFIGURATION
# ==========================================
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")

if REDIS_PASSWORD:
    _redis_url = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}"
else:
    _redis_url = f"redis://{REDIS_HOST}:{REDIS_PORT}"

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            "hosts": [_redis_url],
        },
    },
}
