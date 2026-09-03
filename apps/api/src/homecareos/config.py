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

    # Chaves de API válidas para autenticar requisições a `/api/*`, separadas
    # por vírgula (permite rotação sem downtime: adiciona a nova, troca os
    # clientes, remove a velha). Vazio só é aceito em `environment == "local"`
    # — em qualquer outro ambiente a aplicação recusa subir (ver `main.py`).
    api_keys: str = ""

    # Responsável atribuído a toda pendência que a classificação abre. Não é um
    # id de usuário porque não existe modelo de usuário ainda: a atribuição real
    # a uma pessoa acontece por reatribuição via `PATCH /api/pendencias/{id}`.
    pendencia_responsavel_padrao: str = "equipe-conferencia"

    # Gateway de WhatsApp (uazapi). Base URL vazia OU token vazio desabilita
    # todo o envio de alerta — o sistema segue funcionando, só não notifica.
    uazapi_base_url: str = ""
    uazapi_token: str = ""
    alertas_timeout_segundos: float = 10.0

    # Destinatários por tipo de alerta, JSON:
    #   {"documento_incompleto_critico": ["5521999999999"], ...}
    alertas_destinatarios: str = ""
    # Sobrescrita opcional dos templates, JSON: {"<tipo>": "<template>"}.
    alertas_templates: str = ""

    # Anti-bombardeio: teto por destinatário por hora e intervalo mínimo entre
    # dois alertas sobre o MESMO assunto (ver `alerts/service.py`).
    alertas_max_por_hora_por_destinatario: int = 10
    alertas_cooldown_horas: int = 24

    # Parâmetros dos detectores (ver `alerts/detectores.py`).
    alertas_dias_antes_deadline: int = 3
    alertas_horas_pendencia_parada: int = 48
    alertas_volume_janela_dias: int = 14
    alertas_volume_fator: float = 1.5
    alertas_volume_minimo_documentos: int = 10

    # Dispara o alerta de documento incompleto crítico já na classificação,
    # além da varredura periódica.
    alertas_hook_inline_habilitado: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
