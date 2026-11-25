from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "KoboDesk API"
    database_url: str = "sqlite+aiosqlite:///./kobodesk.db"
    trial_days: int = 5
    weekly_price_ngn: int = 350
    monthly_price_ngn: int = 1500
    paystack_secret_key: str = ""
    paystack_webhook_secret: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
