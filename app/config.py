from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime config, read from environment / .env file."""

    # Entra service principal used to authenticate to the Fabric lakehouse
    # (app -> data access). REST API client auth is handled later by APIM.
    AZURE_TENANT_ID: str = ""
    FABRIC_CLIENT_ID: str | None = None
    # Local dev: set in .env. In Azure: leave empty and set KEY_VAULT_URL; the
    # app fetches it from Key Vault via the App Service managed identity.
    FABRIC_CLIENT_SECRET: SecretStr = SecretStr("")
    # Azure Key Vault holding the SPN secret. Empty in local dev.
    KEY_VAULT_URL: str = ""
    FABRIC_CLIENT_SECRET_NAME: str = "fabric-spn-secret"

    # Fabric Lakehouse SQL analytics endpoint (TDS).
    FABRIC_SQL_SERVER: str = ""
    FABRIC_SQL_DATABASE: str = ""
    FABRIC_SQL_SCOPE: str = "https://database.windows.net/.default"
    ODBC_DRIVER: str = "ODBC Driver 18 for SQL Server"

    APP_NAME: str = "lakehouse-api"
    LOG_LEVEL: str = "INFO"

    ENABLE_DEV_ENDPOINTS: bool = False

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def _reject_placeholder(self):
        """Fail fast with a clear message if the SPN values are still the
        .env.example placeholders. Catches the case where a stale run, an
        unedited .env, or environment variables leave the all-zeros GUID in
        place — so you get an obvious config error instead of a confusing
        auth failure down the line.
        """
        placeholder = "00000000-0000-0000-0000-000000000000"
        if self.AZURE_TENANT_ID == placeholder or self.FABRIC_CLIENT_ID == placeholder:
            raise ValueError(
                "AZURE_TENANT_ID / FABRIC_CLIENT_ID are still the placeholder "
                "values. Put real values in .env, make sure no AZURE_* / "
                "FABRIC_* environment variables are shadowing it (env vars "
                "take precedence over .env), and restart the server."
            )
        return self


settings = Settings()
