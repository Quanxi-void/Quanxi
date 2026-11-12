from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "ManhwaHQ API"
    app_version: str = "0.6.0"
    environment: str = "development"
    api_prefix: str = "/api/v1"
    source_base_url: str = "https://manhwahq.com"
    source_data_api_base_url: str = "https://b.toonking.xyz"
    source_site_key: str = "manhwahq.com"
    source_timeout: float = 25.0
    source_max_retries: int = 2
    source_max_concurrent: int = Field(default=3, ge=1, le=32)
    source_connection_limit: int = Field(default=20, ge=1, le=200)
    source_keepalive_limit: int = Field(default=10, ge=0, le=100)
    source_user_agent: str = (
        "Mozilla/5.0 (compatible; ManhwaHQ-API/0.6; +https://manhwahq.com)"
    )
    cache_ttl_seconds: int = 180
    cache_max_items: int = Field(default=512, ge=32, le=10000)
    cache_max_bytes: int = Field(default=67108864, ge=1048576)
    enable_browser_fallback: bool = False
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    mongodb_uri: str = ""
    mongodb_database: str = "manhwahq"
    mongodb_max_pool_size: int = Field(default=30, ge=1, le=500)
    mongodb_min_pool_size: int = Field(default=0, ge=0, le=100)
    mongodb_wait_queue_timeout_ms: int = Field(default=5000, ge=100, le=60000)
    mongodb_connect_timeout_ms: int = Field(default=5000, ge=100, le=60000)
    mongodb_socket_timeout_ms: int = Field(default=15000, ge=100, le=120000)
    user_activity_touch_interval_seconds: int = Field(default=60, ge=10, le=3600)
    activity_log_ttl_seconds: int = Field(default=1800, ge=60)
    jwt_secret: str = "CHANGE_ME_TO_A_LONG_RANDOM_SECRET_IN_PRODUCTION"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 20
    refresh_token_days: int = 30
    avatar_upload_dir: str = "uploads/avatars"

    # Shared secret sent by the Vercel proxy in the "x-proxy-secret" header. When set, /api/ requests
    # without it are rejected (the health check stays open for uptime monitors). Empty = disabled.
    proxy_secret: str = ""

    # Shared MongoDB-backed token-bucket rate limiting. Every deployment that
    # should share counters must use the same MongoDB cluster and database.
    rate_limit_enabled: bool = True
    rate_limit_fail_open: bool = True
    rate_limit_collection: str = "rate_limits"
    rate_limit_operation_timeout: float = Field(default=1.5, gt=0)
    rate_limit_key_prefix: str = "manhwahq:ratelimit"
    rate_limit_default_per_minute: int = Field(default=90, ge=1)
    rate_limit_discovery_per_minute: int = Field(default=60, ge=1)
    rate_limit_reader_per_minute: int = Field(default=30, ge=1)
    rate_limit_user_per_minute: int = Field(default=90, ge=1)
    rate_limit_login_per_5_minutes: int = Field(default=10, ge=1)
    rate_limit_register_per_hour: int = Field(default=5, ge=1)
    rate_limit_refresh_per_minute: int = Field(default=30, ge=1)

    @property
    def allowed_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
