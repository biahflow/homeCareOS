from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Nome padrão do cookie de sessão de usuário. Vive aqui, e não em `auth/`,
# porque é o default de uma configuração e `config` é a camada mais baixa: a
# declaração do esquema de segurança em `auth/dependencies.py` importa esta
# constante, nunca o contrário.
COOKIE_SESSAO_PADRAO = "homecareos_sessao"


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
    # id de usuário porque a classificação é automática e não tem pessoa: a
    # atribuição a alguém de verdade acontece por reatribuição via
    # `PATCH /api/pendencias/{id}`, que desde a issue #30 aceita
    # `responsavel_id` e vincula a pendência a um usuário cadastrado.
    pendencia_responsavel_padrao: str = "equipe-conferencia"

    # Duração da sessão de usuário. 12h cobre um turno inteiro sem obrigar
    # relogin no meio do fechamento de competência.
    sessao_duracao_horas: int = 12
    # Nome do cookie que carrega o token opaco de sessão. O padrão é também o
    # nome declarado no esquema de segurança do OpenAPI (ver
    # `auth/dependencies.py`): trocar esta configuração troca o cookie de
    # verdade e desatualiza a declaração, então é operação de exceção.
    sessao_cookie_nome: str = COOKIE_SESSAO_PADRAO

    # Freio para força bruta contra POST /api/auth/login (issue #33). Ver
    # `auth/protecao.py` para o desenho completo.
    # Janela de observação das tentativas de login.
    login_janela_minutos: int = 15
    # Falhas na janela que travam a origem (IP). A trava só dispara quando
    # NÃO houve nenhum login bem-sucedido daquele IP na janela: IP
    # compartilhado é o caso comum (atrás de proxy, a empresa inteira chega
    # com um só), e contar falhas cruas trancaria toda a equipe por erros de
    # digitação somados. Ver `auth/protecao.avaliar_bloqueio`.
    login_falhas_para_travar_ip: int = 10
    # Falhas na janela que travam a conta. MUITO mais alto que o de IP, e por
    # quê: travar conta permite que qualquer um que saiba o e-mail de alguém
    # mantenha a pessoa fora do sistema de propósito. É o último recurso, não
    # o primeiro.
    login_falhas_para_travar_conta: int = 20
    login_trava_minutos: int = 15
    # Atraso progressivo. O TETO não é ajuste fino: sem ele, requisições
    # baratas esgotam o threadpool do FastAPI e o próprio atraso vira o
    # ataque.
    login_atraso_base_segundos: float = 0.25
    login_atraso_maximo_segundos: float = 2.0
    # `X-Forwarded-For` só é confiável quando existe um proxy que o reescreve.
    # Default `False`: confiar por padrão deixaria qualquer um forjar a
    # origem e escapar da trava de IP.
    confiar_em_x_forwarded_for: bool = False

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
