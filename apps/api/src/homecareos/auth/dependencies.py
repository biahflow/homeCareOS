"""Quem está chamando (`principal_atual`) e o que essa pessoa pode (`exigir_papel`).

Estas duas dependências substituem `require_api_key` no `include_router(...)` de
`main.py`. Elas **não** substituem a autenticação por chave: `principal_atual`
continua aceitando `X-API-Key` e delega a validação da chave ao próprio
`api/auth.require_api_key` — a comparação em tempo constante do segredo de
máquina continua morando lá, num lugar só.

Ordem de resolução: **sessão primeiro, chave depois**. Um navegador que mande
as duas credenciais é tratado como a pessoa, não como a máquina; invertido, a
auditoria perderia justamente a identidade que a issue #30 existe para
registrar.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyCookie, APIKeyHeader
from sqlalchemy.orm import Session

from homecareos.api.auth import MENSAGEM_CREDENCIAL_INVALIDA, require_api_key
from homecareos.auth.schema import ROTULO_MAQUINA, Papel, Principal
from homecareos.auth.sessoes import resolver_sessao
from homecareos.config import COOKIE_SESSAO_PADRAO, Settings, get_settings
from homecareos.db.session import get_session

# Mensagem do 403. Não nomeia o papel que faltou, de propósito: dizer "exige
# gestor" ensina a quem sondou onde estão os endpoints valiosos e qual papel
# procurar comprometer. Quem tem direito ao acesso descobre o papel com quem
# administra, não com a resposta de erro.
MENSAGEM_SEM_PERMISSAO = "acesso não autorizado para este usuário"

# Os dois esquemas de segurança existem para o OpenAPI declarar as credenciais
# que cada operação aceita — é o que mantém o contrato honesto para o frontend e
# o que faz toda rota de `/api/*` continuar aparecendo como protegida no schema.
# `auto_error=False` nos dois porque quem decide o 401 — e quem garante corpo
# idêntico para credencial ausente e credencial errada — é `principal_atual`.
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_cookie_sessao = APIKeyCookie(name=COOKIE_SESSAO_PADRAO, auto_error=False)


def token_de_sessao(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    _declarado: Annotated[str | None, Depends(_cookie_sessao)] = None,
) -> str | None:
    """Token opaco que veio no cookie de sessão, se veio.

    A leitura autoritativa é `request.cookies[settings.sessao_cookie_nome]`, e
    não o valor de `_declarado`: o nome do cookie é configurável e o esquema do
    OpenAPI precisa de um literal fixo na importação. `_declarado` existe
    exclusivamente para o esquema aparecer no schema — trocar
    `SESSAO_COOKIE_NOME` troca o cookie de verdade e desatualiza a declaração,
    e é por isso que essa troca é operação de exceção (ver `config.py`).
    """
    return request.cookies.get(settings.sessao_cookie_nome)


def _principal_de_maquina(settings: Settings, x_api_key: str | None) -> Principal | None:
    """Principal da integração máquina-a-máquina, ou `None` se a chave não vale.

    A validação é delegada a `require_api_key`: é ela que compara em tempo
    constante e que trata chave não-ASCII sem virar 500. Reimplementar a
    comparação aqui criaria um segundo lugar para ela envelhecer.
    """
    try:
        require_api_key(settings=settings, x_api_key=x_api_key)
    except HTTPException:
        return None
    return Principal(tipo="maquina", usuario_id=None, papel=None, rotulo=ROTULO_MAQUINA)


def principal_atual(
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    token: Annotated[str | None, Depends(token_de_sessao)] = None,
    x_api_key: Annotated[str | None, Depends(_api_key_header)] = None,
) -> Principal:
    """Autentica a requisição e devolve quem a está fazendo.

    Cookie de sessão ausente, cookie expirado, cookie revogado, usuário
    desativado e chave de API errada respondem **exatamente igual**: 401 com a
    mensagem de `api/auth.MENSAGEM_CREDENCIAL_INVALIDA`. A constante é reusada e
    não copiada — a razão de os casos serem indistinguíveis está documentada lá,
    e duplicar a string abriria a porta para os dois textos divergirem e a
    diferença virar sinal para quem sonda.
    """
    if token is not None:
        usuario = resolver_sessao(session, token, agora=datetime.now(UTC))
        if usuario is not None:
            return Principal(
                tipo="usuario",
                usuario_id=usuario.id,
                papel=Papel(usuario.papel),
                rotulo=usuario.email,
            )

    principal_maquina = _principal_de_maquina(settings, x_api_key)
    if principal_maquina is not None:
        return principal_maquina

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=MENSAGEM_CREDENCIAL_INVALIDA,
    )


def papeis_da_chave_de_api(settings: Settings) -> frozenset[Papel]:
    """Os papéis que a `X-API-Key` carrega, lidos de `settings.api_key_papeis`.

    Lista separada por vírgula, como `api_keys`. **Vazio é o default e devolve
    conjunto vazio**: a chave autentica mas não satisfaz `exigir_papel` nenhum.

    Nome desconhecido levanta `ValueError` em vez de ser ignorado. Ignorar um
    typo aqui seria degradar em silêncio para "sem papel nenhum" — a integração
    passaria a tomar 403 em tudo e a resposta não diria por quê. Quem transforma
    esse erro em recusa de subir é `main._validar_configuracao_de_auth`, no
    mesmo lugar que já recusa subir sem `api_keys` fora de `local`.

    A leitura é por requisição, como a de `api/auth._chaves_validas`: é um
    `split` sobre uma configuração de poucos caracteres, e cachear obrigaria a
    invalidar o cache nos testes que trocam `Settings` por override.
    """
    papeis: set[Papel] = set()
    for nome in settings.api_key_papeis.split(","):
        limpo = nome.strip()
        if not limpo:
            continue
        try:
            papeis.add(Papel(limpo))
        except ValueError as exc:
            validos = ", ".join(papel.value for papel in Papel)
            raise ValueError(
                f"api_key_papeis contém {limpo!r}, que não é um papel válido. "
                f"Papéis válidos: {validos} (ou vazio, para a chave não abrir "
                "rota de papel restrito nenhuma)."
            ) from exc
    return frozenset(papeis)


def exigir_papel(*papeis: Papel) -> Callable[..., Principal]:
    """Devolve a dependency que só deixa passar quem tem um dos `papeis`.

    Para **sessão de usuário**, o papel é o da pessoa. Para
    `Principal(tipo="maquina")`, é o que `API_KEY_PAPEIS` declarar (ADR 0007):
    a chave passa se algum papel configurado estiver entre os exigidos, e
    responde 403 caso contrário.

    O default (`api_key_papeis` vazio) é restritivo: a chave continua
    autenticando — chave ausente ou errada é 401, e isso não mudou — mas não
    abre nenhuma rota de papel restrito. Até o ADR 0007 ela passava em qualquer
    checagem, e a justificativa registrada dizia que o cron de alertas dependia
    disso. **Não dependia**: `alerts/scan.py` abre uma sessão do banco e não faz
    requisição HTTP nenhuma — não há header para mandar. A compatibilidade que
    sobrou é a das integrações máquina-a-máquina que a chave sempre permitiu, e
    agora ela é declarada em vez de presumida.

    401 e 403 continuam sendo coisas diferentes aqui: credencial inválida é 401
    e indistinguível entre cookie e chave (ver `principal_atual`); credencial
    válida sem o papel é 403 com `MENSAGEM_SEM_PERMISSAO`, que não nomeia o
    papel que faltou.
    """
    exigidos = frozenset(papeis)

    def dependency(
        principal: Annotated[Principal, Depends(principal_atual)],
        settings: Annotated[Settings, Depends(get_settings)],
    ) -> Principal:
        if principal.tipo == "maquina":
            autorizado = bool(papeis_da_chave_de_api(settings) & exigidos)
        else:
            autorizado = principal.papel in exigidos
        if not autorizado:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=MENSAGEM_SEM_PERMISSAO,
            )
        return principal

    return dependency
