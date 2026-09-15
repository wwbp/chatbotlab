import json
import os
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


STATIC_ROOT = BASE_DIR / "staticfiles"

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.1/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY environment variable is not set")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.getenv("DEBUG", "False") == "True"


def _csv_env(name, default):
    """Read a comma-separated env var, falling back to `default` when unset.

    Deployment-specific hostnames belong in the environment rather than in the
    source of a public repository. These are not secrets — the browser sees
    them on every request — so the fallback keeps the existing deployment
    working unchanged if the variable is not set.
    """
    raw = os.getenv(name, "")
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or default


ALLOWED_HOSTS = (
    ["localhost", "127.0.0.1", "0.0.0.0"]
    if DEBUG
    else _csv_env("ALLOWED_HOSTS", ["dev.bot.wwbp.org", "bot.wwbp.org"])
)

if DEBUG:
    REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")

if not DEBUG:
    REDIS_URL = os.getenv("REDIS_URL")
    if not REDIS_URL:
        raise RuntimeError("REDIS_URL environment variable is not set")

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            # A transient Redis blip should degrade gracefully, not 500 the user.
            # With IGNORE_EXCEPTIONS a failed read returns the default (the chat
            # path then falls back to loading history from the DB), and a failed
            # write is dropped instead of raising "Timeout writing to socket".
            "IGNORE_EXCEPTIONS": True,
            "SOCKET_CONNECT_TIMEOUT": 2,  # seconds to open the connection
            "SOCKET_TIMEOUT": 2,  # seconds per read/write op
            "CONNECTION_POOL_KWARGS": {
                "retry_on_timeout": True,
                # Redis sits nearly idle, yet writes were timing out — the
                # signature of stale/half-open pooled TCP connections being
                # reused. Keepalive + a periodic PING recycle dead connections
                # before they hang a request.
                "socket_keepalive": True,
                "health_check_interval": 30,
            },
        },
    },
}

# Still log the swallowed Redis errors so blips stay visible in the logs.
DJANGO_REDIS_LOG_IGNORED_EXCEPTIONS = True

# Append Elastic Beanstalk Load Balancer Health Check requests since the source host IP address keeps changing
if not DEBUG:
    try:
        token = requests.put(
            "http://169.254.169.254/latest/api/token",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        ).text
        internal_ip = requests.get(
            "http://169.254.169.254/latest/meta-data/local-ipv4",
            headers={"X-aws-ec2-metadata-token": token},
        ).text
    except requests.exceptions.ConnectionError:
        pass
    else:
        ALLOWED_HOSTS.append(internal_ip)
    del requests

# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "django_redis",
    "chatbot",
    "rest_framework",
    "import_export",
    "storages",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "chatbot.middleware.RequestTimingMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:5173",
    *_csv_env(
        "CORS_ALLOWED_ORIGINS", ["https://dev.bot.wwbp.org", "https://bot.wwbp.org"]
    ),
]
_frontend_url_env = os.getenv("FRONTEND_URL", "")
_frontend_url_parsed = urlparse(_frontend_url_env)
_frontend_url_valid = (
    _frontend_url_env
    and _frontend_url_parsed.scheme in ("http", "https")
    and bool(_frontend_url_parsed.netloc)
)
if _frontend_url_valid:
    CORS_ALLOWED_ORIGINS.append(_frontend_url_env)

CSRF_TRUSTED_ORIGINS = [
    "http://localhost:3000",
    *_csv_env(
        "CSRF_TRUSTED_ORIGINS", ["https://dev.bot.wwbp.org", "https://bot.wwbp.org"]
    ),
]
if _frontend_url_valid:
    CSRF_TRUSTED_ORIGINS.append(_frontend_url_env)

ROOT_URLCONF = "generic_chatbot.urls"


ASGI_APPLICATION = "generic_chatbot.asgi.application"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Shared secret the survey tool sends as the X-Survey-Token header when
# POSTing participant answers to /api/survey_response/. Unset means the
# endpoint rejects every request (fail closed).
SURVEY_INGEST_TOKEN = os.getenv("SURVEY_INGEST_TOKEN", "")
MODERATION_VALUES_FOR_BLOCKED = json.loads(
    os.environ.get(
        "MODERATION_VALUES_FOR_BLOCKED",
        """{
            "harassment": 0.5,
            "harassment/threatening": 0.1,
            "hate": 0.5,
            "hate/threatening": 0.1,
            "self-harm": 0.2,
            "self-harm/instructions": 0.5,
            "self-harm/intent": 0.7,
            "sexual": 0.5,
            "sexual/minors": 0.2,
            "violence": 0.7,
            "violence/graphic": 0.8
        }""",
    ),
)

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR, "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "generic_chatbot.wsgi.application"


# Database
# https://docs.djangoproject.com/en/5.1/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.getenv("DATABASE_NAME"),
        "USER": os.getenv("DATABASE_USER"),
        "PASSWORD": os.getenv("DATABASE_PASSWORD"),
        "HOST": os.getenv("DATABASE_HOST"),
        "PORT": os.getenv("DATABASE_PORT"),
        # CONN_MAX_AGE=0: open/close one connection per request.
        # Required for Django async (ASGI) with MySQL: asgiref's thread_sensitive=True
        # serialises all ORM calls through a single shared thread per worker, so
        # reusing connections (CONN_MAX_AGE>0) causes state corruption under concurrent
        # async load (80%+ failures at ≥5 concurrent requests on staging).
        # Long-term: migrate to aiomysql/asyncmy or add ProxySQL for true async pooling.
        "CONN_MAX_AGE": int(os.getenv("CONN_MAX_AGE", "0")),
        # CONN_HEALTH_CHECKS=True: PING a pooled connection and transparently
        # reconnect if the server already closed it, instead of raising
        # (2006, 'Server has gone away'). Under the ASGI/thread-sensitive stack a
        # connection can linger across an idle gap and go stale; without the health
        # check the first query on that dead socket 500s. Symptom mirrors the Redis
        # stale-connection bug fixed in CACHES above, on the DB side.
        "CONN_HEALTH_CHECKS": True,
    },
}


# Password validation
# https://docs.djangoproject.com/en/5.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.1/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.1/howto/static-files/

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Add STATICFILES_DIRS for development
if DEBUG:
    STATICFILES_DIRS = [
        BASE_DIR / "static",
    ]

# WhiteNoise configuration
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = DEBUG
WHITENOISE_MAX_AGE = 31536000 if not DEBUG else 0
WHITENOISE_INDEX_FILE = True

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Storage backends — Django 5.1 requires the STORAGES dict.
# STATICFILES_STORAGE and DEFAULT_FILE_STORAGE were removed in 5.1.
#
# staticfiles: WhiteNoise serves static files (JS, CSS) from the container
#   filesystem in both environments. collectstatic writes to /app/staticfiles/
#   and nginx serves them directly — no S3 involved for static files.
#
# default (media/uploads): locally I use the filesystem so development works
#   without AWS credentials. In production I route all FileField writes to S3
#   so voice audio and uploads survive EB instance replacements. The EC2
#   instance role handles AWS authentication — no keys are hardcoded.
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.StaticFilesStorage",
    },
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": os.getenv("AWS_BUCKET_NAME"),
            "region_name": os.getenv("AWS_REGION", "us-east-1"),
            "default_acl": None,
            "file_overwrite": False,
        },
    }
    if not DEBUG
    else {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
}

X_FRAME_OPTIONS = "SAMEORIGIN"
SESSION_COOKIE_SAMESITE = "None"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SAMESITE = "None"
CSRF_COOKIE_SECURE = True

# File upload settings
FILE_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024  # 50MB
DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024  # 50MB
FILE_UPLOAD_TEMP_DIR = None
FILE_UPLOAD_PERMISSIONS = 0o644
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o755

# Environment settings
BACKEND_ENVIRONMENT = os.getenv("BACKEND_ENVIRONMENT", "production")

# Create logs directory if it doesn't exist
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {process:d} {thread:d} {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
        "detailed": {
            "format": "[{levelname}] {asctime} {name} {funcName}:{lineno} - {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "detailed",
            "level": "DEBUG" if DEBUG else "INFO",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "logs" / "django.log",
            "maxBytes": 1024 * 1024 * 10,  # 10MB
            "backupCount": 5,
            "formatter": "detailed",
            "level": "INFO",
        },
        "error_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "logs" / "error.log",
            "maxBytes": 1024 * 1024 * 10,  # 10MB
            "backupCount": 5,
            "formatter": "detailed",
            "level": "ERROR",
        },
    },
    "loggers": {
        "django": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console", "file", "error_file"],
            "level": "ERROR",
            "propagate": False,
        },
        "django.server": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
        "chatbot": {
            "handlers": ["console", "file", "error_file"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
        "chatbot.views": {
            "handlers": ["console", "file", "error_file"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
        "chatbot.services": {
            "handlers": ["console", "file", "error_file"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
        "chatbot.admin": {
            "handlers": ["console", "file", "error_file"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
        "server": {
            "handlers": ["console", "file", "error_file"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
    },
    "root": {
        "handlers": ["console", "file", "error_file"],
        "level": "INFO",
    },
}
