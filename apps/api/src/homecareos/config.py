from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuração da aplicação, lida de variáveis de ambiente / `.env`."""

    # Ambiente de execução: `local` / `homolog` / `production`.
    environment: str = "local"

    # Banco de dados (Postgres 17, driver psycopg 3).
    database_url: str = "postgresql+psycopg://homecareos:homecareos@localhost:5434/homecareos"

    # Storage de documentos (evoluções escaneadas). MinIO em dev; endpoint
    # vazio significa S3 real (a URL padrão do boto3 é usada nesse caso).
    s3_endpoint_url: str = "http://localhost:9002"
    s3_bucket: str = "homecareos-documentos"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_region: str = "us-east-1"

    # Extração assistida por IA (Claude). Chave vazia desabilita a extração.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"

    # Teto de custo por lote de extração (ex.: um PDF grande no fechamento de
    # competência) e custo estimado de cada chamada de Vision, para reserva
    # pessimista antes de cada chamada (ver `extraction/budget.py`).
    extraction_max_cost_usd_per_batch: float = 5.0
    extraction_cost_per_call_usd: float = 0.05

    # Limites de upload e renderização de PDF para conferência visual.
    max_upload_bytes: int = 32 * 1024 * 1024
    pdf_render_dpi: int = 200

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
