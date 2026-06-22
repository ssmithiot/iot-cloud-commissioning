from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = Field(
        default="sqlite:///./cloud-api-dev.db",
        validation_alias=AliasChoices("DATABASE_URL", "CLOUD_DATABASE_URL"),
    )
    auto_create_tables: bool = Field(
        default=True,
        validation_alias=AliasChoices("AUTO_CREATE_TABLES", "CLOUD_AUTO_CREATE_TABLES"),
    )

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        extra="ignore",
    )


settings = Settings()
