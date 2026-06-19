from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./cloud-api-dev.db"
    auto_create_tables: bool = True

    model_config = SettingsConfigDict(
        env_prefix="CLOUD_",
        env_file=".env",
        extra="ignore",
    )


settings = Settings()

