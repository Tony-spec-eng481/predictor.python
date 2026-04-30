"""
Flask configuration classes.
Set FLASK_ENV=production in your environment or .env file.
"""
import os
from datetime import timedelta

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class BaseConfig:
    """Shared settings across all environments."""
    SECRET_KEY = os.environ.get('SECRET_KEY', 'change-me-before-deploy')
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # SocketIO
    SOCKETIO_ASYNC_MODE = 'gevent'

    # CORS — comma-separated list of allowed origins, e.g.:
    #   CORS_ORIGINS=https://your-frontend.vercel.app,https://yoursite.com
    CORS_ORIGINS = os.environ.get('CORS_ORIGINS', '*')

    # SQLite DB path (can be overridden by DATABASE_URL for Postgres, etc.)
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        'DATABASE_URL',
        f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'database.db')}"
    )

    # Logging
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')

    # Gunicorn / server
    HOST = os.environ.get('HOST', '0.0.0.0')
    PORT = int(os.environ.get('PORT', 5000))


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    SQLALCHEMY_ECHO = False  # set True to log all SQL queries


class ProductionConfig(BaseConfig):
    DEBUG = False
    TESTING = False

    # Enforce a real secret key in production
    @classmethod
    def validate(cls):
        if cls.SECRET_KEY == 'change-me-before-deploy':
            raise RuntimeError(
                "SECRET_KEY env var must be set to a random value in production."
            )


class TestingConfig(BaseConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'


# Registry — select via FLASK_ENV env var
config_map = {
    'development': DevelopmentConfig,
    'production':  ProductionConfig,
    'testing':     TestingConfig,
}


def get_config():
    env = os.environ.get('FLASK_ENV', 'development').lower()
    cfg = config_map.get(env, DevelopmentConfig)
    if env == 'production':
        cfg.validate()
    return cfg
