"""Composição da aplicação FastAPI: routers, autenticação e tratamento de erro.

`create_app()` é uma factory (em vez de um único `app` module-level fixo) para
que o teste do critério de aceite 3 — `api_keys` vazio com
`environment="production"` recusa subir — consiga construir uma aplicação a
partir de `Settings` arbitrárias sem depender de variável de ambiente do
processo de teste.
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI

from homecareos.api.auth import require_api_key
from homecareos.api.errors import register_exception_handlers
from homecareos.api.routers.documentos import router as documentos_router
from homecareos.api.routers.operadoras import router as operadoras_router
from homecareos.api.routers.pacientes import router as pacientes_router
from homecareos.api.routers.pendencias import router as pendencias_router
from homecareos.config import Settings, get_settings
from homecareos.intake.router import router as intake_router
from homecareos.reports.router import router as relatorios_router
from homecareos.rules.router import router as rules_router

logger = logging.getLogger(__name__)


def _validar_configuracao_de_auth(settings: Settings) -> None:
    """Recusa subir sem chave configurada fora de `local`.

    Em produção (ou qualquer ambiente que não seja `local`), "sem chave
    configurada" nunca pode significar silenciosamente "sem autenticação" —
    é melhor a aplicação não subir do que subir com `/api/*` aberto.
    """
    tem_chave = bool(settings.api_keys.strip())
    if tem_chave:
        return
    if settings.environment != "local":
        raise RuntimeError(
            "settings.api_keys está vazio e environment="
            f"{settings.environment!r} (!= 'local'). A aplicação recusa subir "
            "sem nenhuma API key configurada fora do ambiente local."
        )
    logger.warning(
        "settings.api_keys está vazio em environment='local': toda rota "
        "/api/* vai rejeitar qualquer X-API-Key com 401, porque nenhuma "
        "chave é válida. Configure API_KEYS no .env para autenticar."
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings if settings is not None else get_settings()
    _validar_configuracao_de_auth(resolved_settings)

    app = FastAPI(
        title="HomeCareOS API",
        description=(
            "API de conferência pré-faturamento do HomeCareOS. Toda rota sob "
            "`/api/*` exige o header `X-API-Key`; `/health` é a única exceção "
            "(sonda de infraestrutura)."
        ),
    )

    register_exception_handlers(app)

    app.include_router(intake_router, dependencies=[Depends(require_api_key)])
    app.include_router(documentos_router, dependencies=[Depends(require_api_key)])
    app.include_router(pendencias_router, dependencies=[Depends(require_api_key)])
    app.include_router(operadoras_router, dependencies=[Depends(require_api_key)])
    app.include_router(pacientes_router, dependencies=[Depends(require_api_key)])
    # O router de regras nasce sem auth própria (é escrito pela trilha do motor
    # de regras); a proteção é aplicada aqui, como para todos os outros.
    app.include_router(rules_router, dependencies=[Depends(require_api_key)])
    app.include_router(relatorios_router, dependencies=[Depends(require_api_key)])

    @app.get("/health", summary="Sonda de infraestrutura", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
