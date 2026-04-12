"""
ChannelView - Configuration Management
Environment-aware config for dev, staging, and production.

Usage:
    from config import config
    config.SECRET_KEY
    config.S3_BUCKET
    config.SENDGRID_API_KEY
"""
import os
import secrets


class BaseConfig:
    """Shared config across all environments."""
    APP_NAME = 'ChannelView'
    VERSION = '0.8.0'

    # Security
    SECRET_KEY = os.environ.get('SECRET_KEY', 'channelview-dev-secret-change-in-prod')
    JWT_EXPIRY_DAYS = int(os.environ.get('JWT_EXPIRY_DAYS', '30'))

    # Database
    DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(__file__), 'channelview.db'))

    # File uploads
    MAX_UPLOAD_MB = int(os.environ.get('MAX_UPLOAD_MB', '500'))
    MAX_RESPONSE_MB = int(os.environ.get('MAX_RESPONSE_MB', '100'))
    UPLOAD_DIR = os.environ.get('UPLOAD_DIR', os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'videos'))
    INTRO_DIR = os.environ.get('INTRO_DIR', os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'intros'))

    # Video storage: 'local' or 's3'
    STORAGE_BACKEND = os.environ.get('STORAGE_BACKEND', 'local')

    # S3 configuration (only used when STORAGE_BACKEND='s3')
    S3_BUCKET = os.environ.get('S3_BUCKET', '')
    S3_REGION = os.environ.get('S3_REGION', 'us-east-1')
    S3_ACCESS_KEY = os.environ.get('S3_ACCESS_KEY', '')
    S3_SECRET_KEY = os.environ.get('S3_SECRET_KEY', '')
    S3_ENDPOINT = os.environ.get('S3_ENDPOINT', '')  # For S3-compatible (MinIO, R2, etc.)
    S3_PREFIX = os.environ.get('S3_PREFIX', 'videos/')
    S3_PRESIGN_EXPIRY = int(os.environ.get('S3_PRESIGN_EXPIRY', '3600'))  # 1 hour

    # Email: 'sendgrid', 'smtp', or 'log'
    EMAIL_BACKEND = os.environ.get('EMAIL_BACKEND', 'smtp')
    SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
    SENDGRID_FROM_EMAIL = os.environ.get('SENDGRID_FROM_EMAIL', '')
    SENDGRID_FROM_NAME = os.environ.get('SENDGRID_FROM_NAME', 'ChannelView')

    # CORS
    CORS_ORIGINS = os.environ.get('CORS_ORIGINS', '*')

    # Branding defaults
    DEFAULT_BRAND_COLOR = '#0ace0a'
    DEFAULT_AGENCY_NAME = 'ChannelView'

    @property
    def MAX_CONTENT_LENGTH(self):
        return self.MAX_UPLOAD_MB * 1024 * 1024

    @property
    def MAX_RESPONSE_SIZE(self):
        return self.MAX_RESPONSE_MB * 1024 * 1024


class DevConfig(BaseConfig):
    """Development configuration."""
    ENV = 'development'
    DEBUG = True
    SEND_FILE_MAX_AGE_DEFAULT = 0
    TEMPLATES_AUTO_RELOAD = True
    LOG_LEVEL = 'DEBUG'

    # In dev, fall back to log-only email if no SMTP configured
    EMAIL_FALLBACK = 'log'


class StagingConfig(BaseConfig):
    """Staging configuration."""
    ENV = 'staging'
    DEBUG = False
    SEND_FILE_MAX_AGE_DEFAULT = 3600  # 1 hour cache
    TEMPLATES_AUTO_RELOAD = False
    LOG_LEVEL = 'INFO'
    EMAIL_FALLBACK = 'log'

    def __init__(self):
        if self.SECRET_KEY == 'channelview-dev-secret-change-in-prod':
            self.SECRET_KEY = secrets.token_hex(32)
            print("[WARNING] No SECRET_KEY set for staging — generated random key.")


class ProdConfig(BaseConfig):
    """Production configuration."""
    ENV = 'production'
    DEBUG = False
    SEND_FILE_MAX_AGE_DEFAULT = 31536000  # 1 year cache
    TEMPLATES_AUTO_RELOAD = False
    LOG_LEVEL = 'WARNING'
    EMAIL_FALLBACK = 'none'  # In prod, email must actually send

    # Force HTTPS
    PREFERRED_URL_SCHEME = 'https'

    def __init__(self):
        if self.SECRET_KEY == 'channelview-dev-secret-change-in-prod':
            self.SECRET_KEY = secrets.token_hex(32)
            print("[CRITICAL] No SECRET_KEY set for production — generated random key. Set SECRET_KEY env var!")

        # Validate critical production settings
        warnings = []
        if not self.S3_BUCKET and self.STORAGE_BACKEND == 's3':
            warnings.append("S3_BUCKET not set but STORAGE_BACKEND=s3")
        if not self.SENDGRID_API_KEY and self.EMAIL_BACKEND == 'sendgrid':
            warnings.append("SENDGRID_API_KEY not set but EMAIL_BACKEND=sendgrid")
        for w in warnings:
            print(f"[PROD WARNING] {w}")


# Environment selector
_configs = {
    'development': DevConfig,
    'staging': StagingConfig,
    'production': ProdConfig,
}


def get_config():
    """Get config for current environment. Set FLASK_ENV or CHANNELVIEW_ENV."""
    env = os.environ.get('CHANNELVIEW_ENV') or os.environ.get('FLASK_ENV', 'development')
    cfg_class = _configs.get(env, DevConfig)
    return cfg_class() if env != 'development' else cfg_class()


config = get_config()
