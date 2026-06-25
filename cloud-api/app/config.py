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
    gateway_auth_pepper: str = Field(min_length=1, validation_alias="GATEWAY_AUTH_PEPPER")
    admin_api_token: str = Field(min_length=1, validation_alias="IOT_ADMIN_API_TOKEN")

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        extra="ignore",
    )


settings = Settings()
