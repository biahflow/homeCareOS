"""Composição da aplicação FastAPI: routers, autorização e tratamento de erro.

`create_app()` é uma factory (em vez de um único `app` module-level fixo) para
que o teste do critério de aceite 3 — `api_keys` vazio com
`environment="production"` recusa subir — consiga construir uma aplicação a
partir de `Settings` arbitrárias sem depender de variável de ambiente do
processo de teste.

**A autorização por papel é aplicada aqui, por router** (issue #30), no lugar
onde antes ficava `require_api_key`: um endpoint novo nasce protegido por
construção, sem depender de alguém lembrar de proteger cada rota — é a mesma
regra que a docstring de `api/auth.py` já justificava.

`exigir_papel(...)` **não** substitui a chave de API: ela continua válida e
continua dando acesso total, porque é a credencial máquina-a-máquina de que o
cron `python -m homecareos.alerts.scan` depende. Papel só filtra sessão de
usuário — a justificativa completa está na docstring de
`auth/dependencies.exigir_papel`.

Onde um router mistura capacidades de papéis diferentes, o router leva a regra
mais larga e o endpoint restritivo leva a sua própria, declarada nele mesmo
(`POST /api/documentos/{id}/revalidar`, `PATCH /api/pendencias/{id}` e os
relatórios de gestão). É exceção consciente à regra "auth por router", anotada
em cada endpoint.
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI

from homecareos.alerts.router import router as alertas_router
from homecareos.api.errors import register_exception_handlers
from homecareos.api.routers.documentos import router as documentos_router
from homecareos.api.routers.operadoras import router as operadoras_router
from homecareos.api.routers.pacientes import router as pacientes_router
from homecareos.api.routers.pendencias import router as pendencias_router
from homecareos.auth.auditoria_router import router as auditoria_usuarios_router
from homecareos.auth.dependencies import exigir_papel
from homecareos.auth.router import router as auth_router
from homecareos.auth.schema import Papel
from homecareos.auth.usuarios_router import router as usuarios_router
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
            "`/api/*` exige credencial — sessão de usuário (cookie `httpOnly`, "
            "obtida em `POST /api/auth/login`) ou o header `X-API-Key` da "
            "integração máquina-a-máquina. `/health` e `POST /api/auth/login` "
            "são as únicas exceções."
        ),
    )

    register_exception_handlers(app)

    todos_os_papeis = (Papel.CONFERENTE, Papel.COORDENADOR, Papel.GESTOR)

    # `POST /api/auth/login` é a única rota de `/api/*` que nasce sem exigir
    # credencial, e a razão é a que parece: não dá para exigir sessão para criar
    # sessão. `/logout` acompanha o mesmo router porque um logout que respondesse
    # 401 para cookie expirado deixaria o navegador preso com um cookie que ele
    # não consegue nem descartar; `GET /api/auth/eu` exige credencial por conta
    # própria, na dependency do endpoint.
    app.include_router(auth_router)
    # Administração de usuários (issue #30, ADR 0004): as três rotas são do
    # coordenador. A regra fica aqui, e não endpoint a endpoint, porque é
    # justamente neste router que um endpoint novo precisa nascer protegido por
    # construção — quem cria usuário decide quem entra. A recusa de atribuir o
    # papel `gestor` continua sendo do endpoint: ela é sobre o papel atribuído,
    # não sobre quem chama, e vale inclusive para a chave de máquina, que esta
    # linha (como todas as outras) deixa passar.
    app.include_router(usuarios_router, dependencies=[Depends(exigir_papel(Papel.COORDENADOR))])
    # Leitura da auditoria administrativa (issue #30, fecha o ADR 0004): router
    # próprio (ver a docstring de `auth/auditoria_router.py`), mesma restrição
    # de papel dos dados que ela expõe.
    app.include_router(
        auditoria_usuarios_router, dependencies=[Depends(exigir_papel(Papel.COORDENADOR))]
    )

    app.include_router(
        intake_router,
        dependencies=[Depends(exigir_papel(Papel.CONFERENTE, Papel.COORDENADOR))],
    )
    # Ler documento é dos três; revalidar é ação de conferência e declara a
    # restrição no próprio endpoint.
    app.include_router(documentos_router, dependencies=[Depends(exigir_papel(*todos_os_papeis))])
    # Ler pendência é dos três; transicionar declara a restrição no endpoint.
    app.include_router(pendencias_router, dependencies=[Depends(exigir_papel(*todos_os_papeis))])
    app.include_router(operadoras_router, dependencies=[Depends(exigir_papel(*todos_os_papeis))])
    # `POST /api/pacientes` não consta da matriz aprovada e por isso herda a
    # regra do router (os três papéis), que é o comportamento que já existia com
    # a chave de API. Estreitá-lo sem o cliente seria inventar requisito.
    app.include_router(pacientes_router, dependencies=[Depends(exigir_papel(*todos_os_papeis))])
    # O router de regras nasce sem auth própria (é escrito pela trilha do motor
    # de regras); a proteção é aplicada aqui, como para todos os outros. Regra de
    # glosa é do coordenador: é ela que decide o que reprova um documento.
    app.include_router(rules_router, dependencies=[Depends(exigir_papel(Papel.COORDENADOR))])
    # Relatório operacional é dos três; métricas e baseline declaram a restrição
    # nos próprios endpoints.
    app.include_router(relatorios_router, dependencies=[Depends(exigir_papel(*todos_os_papeis))])
    app.include_router(
        alertas_router,
        dependencies=[Depends(exigir_papel(Papel.COORDENADOR, Papel.GESTOR))],
    )

    @app.get("/health", summary="Sonda de infraestrutura", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
