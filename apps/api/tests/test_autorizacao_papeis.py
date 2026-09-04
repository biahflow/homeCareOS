"""Testes de integração da matriz de papéis e da auditoria com nome — issue #30.

Dois blocos:

1. **Autorização** — exercita a matriz aprovada (ADR 0001) com um usuário de
   cada papel, e trava a compatibilidade que a issue exige: `X-API-Key` passa em
   todas as rotas, inclusive nas de papel restrito. Sem esse teste, alguém
   "aperta" a chave um dia e derruba o cron de alertas em produção.
2. **Auditoria** — o critério de aceite nº 1: duas pessoas diferentes
   transicionando pendências produzem duas linhas de `log_conferencia` com
   `usuario` distinto e o `usuario_id` correspondente.

O banco é compartilhado com o desenvolvimento: tudo o que estes testes criam
(usuários, sessões, operadora, documentos, pendências, logs, regras e baseline)
é apagado no teardown, e nenhuma asserção conta linhas sem filtrar pelos
próprios registros.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from homecareos.auth import senhas
from homecareos.auth.dependencies import MENSAGEM_SEM_PERMISSAO
from homecareos.auth.schema import Papel
from homecareos.config import Settings, get_settings
from homecareos.db.models import (
    Documento,
    DocumentoStatus,
    LogConferencia,
    Operadora,
    Pendencia,
    PendenciaStatus,
    TipoDocumento,
    Usuario,
)
from homecareos.db.session import get_sessionmaker
from homecareos.intake.router import get_document_storage
from homecareos.main import app
from tests.conftest import AUTH_HEADERS, TEST_API_KEY
from tests.fakes import FakeStorage

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2
SENHA_DE_TESTE = "senha-de-teste-papeis"
COMPETENCIA_TESTE = "2099-11"


def _postgres_responde(settings: Settings) -> str | None:
    try:
        engine = create_engine(
            settings.database_url, connect_args={"connect_timeout": SONDA_TIMEOUT}
        )
        try:
            with engine.connect() as conexao:
                conexao.execute(text("select 1"))
        finally:
            engine.dispose()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


@pytest.fixture(scope="module")
def settings() -> Settings:
    resolved = get_settings()
    motivo = _postgres_responde(resolved)
    if motivo is not None:
        pytest.skip(f"Postgres indisponível em {resolved.database_url}: {motivo}")
    return resolved


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    """Cliente sem credencial nenhuma; `environment="local"` para o cookie não sair `Secure`."""
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"api_keys": TEST_API_KEY, "environment": "local"}
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def sessao() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def usuarios(sessao: Session) -> Iterator[dict[Papel, Usuario]]:
    """Um usuário de cada papel, com e-mail único (o banco é compartilhado)."""
    criados = {
        papel: Usuario(
            nome=f"Pessoa {papel.value}",
            email=f"{papel.value}-{uuid.uuid4()}@teste.local",
            senha_hash=senhas.gerar_hash(SENHA_DE_TESTE),
            papel=papel.value,
        )
        for papel in Papel
    }
    sessao.add_all(list(criados.values()))
    sessao.commit()

    yield criados

    ids = [usuario.id for usuario in criados.values()]
    sessao.execute(text("delete from sessoes where usuario_id = any(:ids)"), {"ids": ids})
    sessao.execute(text("delete from usuarios where id = any(:ids)"), {"ids": ids})
    sessao.commit()


@pytest.fixture
def clientes(
    settings: Settings, usuarios: dict[Papel, Usuario]
) -> Iterator[dict[Papel, TestClient]]:
    """Um `TestClient` por papel, cada um já com o cookie de sessão do seu login."""
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"api_keys": TEST_API_KEY, "environment": "local"}
    )
    try:
        logados = {}
        for papel, usuario in usuarios.items():
            cliente = TestClient(app)
            resposta = cliente.post(
                "/api/auth/login", json={"email": usuario.email, "senha": SENHA_DE_TESTE}
            )
            assert resposta.status_code == 200, resposta.text
            logados[papel] = cliente
        yield logados
    finally:
        app.dependency_overrides.clear()


@dataclass
class Cenario:
    """Uma operadora própria, dois documentos em `problema` e a pendência de cada um."""

    operadora: Operadora
    documentos: list[Documento]
    pendencias: list[Pendencia]


@pytest.fixture
def cenario(sessao: Session) -> Iterator[Cenario]:
    operadora = Operadora(nome="Operadora Papéis", codigo=f"PAPEIS-{uuid.uuid4()}")
    sessao.add(operadora)
    sessao.flush()

    documentos = [
        Documento(
            operadora_id=operadora.id,
            tipo=TipoDocumento.EVOLUCAO,
            arquivo_url=f"s3://fake/papeis/{indice}",
            competencia=COMPETENCIA_TESTE,
            # `problema` de propósito: é o estado a partir do qual a transição da
            # pendência para `em_correcao` propaga para o documento e produz a
            # linha de `log_conferencia` que a auditoria precisa.
            status=DocumentoStatus.PROBLEMA,
        )
        for indice in (1, 2)
    ]
    sessao.add_all(documentos)
    sessao.flush()

    pendencias = [
        Pendencia(
            documento_id=documento.id,
            tipo_problema="campo_ausente",
            campo="carimbo_legivel",
            descricao="falta carimbo legível",
            responsavel="equipe-conferencia",
            status=PendenciaStatus.ABERTA,
            deadline=datetime.now(UTC) + timedelta(days=5),
        )
        for documento in documentos
    ]
    sessao.add_all(pendencias)
    sessao.commit()

    yield Cenario(operadora=operadora, documentos=documentos, pendencias=pendencias)

    ids = [documento.id for documento in documentos]
    for tabela in ("pendencias", "validacoes", "extracoes", "log_conferencia"):
        sessao.execute(text(f"delete from {tabela} where documento_id = any(:ids)"), {"ids": ids})
    sessao.execute(text("delete from documentos where id = any(:ids)"), {"ids": ids})
    sessao.execute(text("delete from regras where operadora_id = :id"), {"id": operadora.id})
    sessao.execute(
        text("delete from baselines_competencia where operadora_id = :id"), {"id": operadora.id}
    )
    sessao.execute(text("delete from operadoras where id = :id"), {"id": operadora.id})
    sessao.commit()


def _corpo_regra(operadora_id: uuid.UUID) -> dict[str, object]:
    return {
        "operadora_id": str(operadora_id),
        "campo": "carimbo_legivel",
        "condicao": {"tipo": "verdadeiro"},
        "acao": "sinalizar",
        "motivo_glosa": "Carimbo ilegível",
    }


def _corpo_baseline(operadora_id: uuid.UUID) -> dict[str, object]:
    return {
        "competencia": COMPETENCIA_TESTE,
        "operadora_id": str(operadora_id),
        "documentos_enviados": 10,
        "documentos_glosados": 2,
        "fonte": "teste de autorização",
    }


# --- a matriz de papéis (ADR 0001, §7 do handoff) ------------------------------


def test_gestor_nao_transiciona_pendencia(
    clientes: dict[Papel, TestClient], cenario: Cenario
) -> None:
    """O gestor lê a operação inteira; executá-la é de quem confere."""
    resposta = clientes[Papel.GESTOR].patch(
        f"/api/pendencias/{cenario.pendencias[0].id}", json={"status": "em_correcao"}
    )

    assert resposta.status_code == 403


def test_conferente_nao_escreve_baseline(
    clientes: dict[Papel, TestClient], cenario: Cenario
) -> None:
    """Baseline é a régua contra a qual a conferência é medida: quem confere não mexe nela."""
    resposta = clientes[Papel.CONFERENTE].put(
        "/api/relatorios/baseline", json=_corpo_baseline(cenario.operadora.id)
    )

    assert resposta.status_code == 403


def test_conferente_nao_cria_regra(clientes: dict[Papel, TestClient], cenario: Cenario) -> None:
    resposta = clientes[Papel.CONFERENTE].post(
        "/api/regras", json=_corpo_regra(cenario.operadora.id)
    )

    assert resposta.status_code == 403


def test_coordenador_cria_regra(clientes: dict[Papel, TestClient], cenario: Cenario) -> None:
    resposta = clientes[Papel.COORDENADOR].post(
        "/api/regras", json=_corpo_regra(cenario.operadora.id)
    )

    assert resposta.status_code == 201


def test_gestor_escreve_baseline(clientes: dict[Papel, TestClient], cenario: Cenario) -> None:
    resposta = clientes[Papel.GESTOR].put(
        "/api/relatorios/baseline", json=_corpo_baseline(cenario.operadora.id)
    )

    assert resposta.status_code == 200


def test_os_tres_papeis_leem_o_relatorio_de_conferencia(
    clientes: dict[Papel, TestClient],
) -> None:
    for papel, cliente in clientes.items():
        resposta = cliente.get(f"/api/relatorios/conferencia?competencia={COMPETENCIA_TESTE}")
        assert resposta.status_code == 200, f"{papel.value} não conseguiu ler o relatório"


def test_os_tres_papeis_veem_o_documento_escaneado(
    clientes: dict[Papel, TestClient], cenario: Cenario
) -> None:
    """Servir o arquivo é leitura de documento, e ler documento é dos três (issue #51).

    O storage é substituído por um fake aqui — o que este teste guarda é a
    autorização da rota nova, não a integração com o MinIO (essa vive em
    `tests/test_api_documento_arquivo.py`, contra storage real).
    """
    documento = cenario.documentos[0]
    conteudo = b"pagina-escaneada"
    app.dependency_overrides[get_document_storage] = lambda: FakeStorage(
        objetos={documento.arquivo_url: (conteudo, "image/png")}
    )

    for papel, cliente in clientes.items():
        resposta = cliente.get(f"/api/documentos/{documento.id}/arquivo")
        assert resposta.status_code == 200, f"{papel.value} não conseguiu ver o documento"
        assert resposta.content == conteudo


def test_conferente_transiciona_pendencia(
    clientes: dict[Papel, TestClient], cenario: Cenario
) -> None:
    resposta = clientes[Papel.CONFERENTE].patch(
        f"/api/pendencias/{cenario.pendencias[0].id}", json={"status": "em_correcao"}
    )

    assert resposta.status_code == 200


def test_metricas_sao_de_coordenador_e_gestor(
    clientes: dict[Papel, TestClient],
) -> None:
    """Métrica agregada é leitura de gestão: coordenador e gestor entram, conferente não."""
    assert clientes[Papel.GESTOR].get("/api/relatorios/metricas").status_code == 200
    assert clientes[Papel.COORDENADOR].get("/api/relatorios/metricas").status_code == 200
    assert clientes[Papel.CONFERENTE].get("/api/relatorios/metricas").status_code == 403


def test_conferente_nao_le_o_log_de_alertas(clientes: dict[Papel, TestClient]) -> None:
    assert clientes[Papel.CONFERENTE].get("/api/alertas").status_code == 403


def test_a_mensagem_do_403_nao_nomeia_o_papel_exigido(
    clientes: dict[Papel, TestClient], cenario: Cenario
) -> None:
    """Dizer "exige gestor" ensina a quem sondou qual papel procurar comprometer."""
    resposta = clientes[Papel.CONFERENTE].put(
        "/api/relatorios/baseline", json=_corpo_baseline(cenario.operadora.id)
    )

    assert resposta.status_code == 403
    corpo = resposta.json()["error"]
    assert corpo["tipo"] == "forbidden"
    assert corpo["mensagem"] == MENSAGEM_SEM_PERMISSAO
    texto = resposta.text.lower()
    for papel in Papel:
        assert papel.value not in texto


# --- compatibilidade: a chave de API continua passando em tudo -----------------


def test_x_api_key_passa_em_todas_as_rotas_de_papel_restrito(
    api: TestClient, cenario: Cenario
) -> None:
    """A compatibilidade que a issue exige, com teste para não ser quebrada por engano.

    `X-API-Key` é a credencial máquina-a-máquina, e o cron
    `python -m homecareos.alerts.scan` depende dela. Papel só filtra sessão de
    usuário — ver `auth/dependencies.exigir_papel`.
    """
    assert api.get("/api/relatorios/conferencia", headers=AUTH_HEADERS).status_code == 200
    assert api.get("/api/relatorios/metricas", headers=AUTH_HEADERS).status_code == 200
    assert api.get("/api/relatorios/baseline", headers=AUTH_HEADERS).status_code == 200
    assert (
        api.put(
            "/api/relatorios/baseline",
            json=_corpo_baseline(cenario.operadora.id),
            headers=AUTH_HEADERS,
        ).status_code
        == 200
    )
    assert (
        api.post(
            "/api/regras", json=_corpo_regra(cenario.operadora.id), headers=AUTH_HEADERS
        ).status_code
        == 201
    )
    assert api.get("/api/alertas", headers=AUTH_HEADERS).status_code == 200
    assert (
        api.patch(
            f"/api/pendencias/{cenario.pendencias[0].id}",
            json={"status": "em_correcao"},
            headers=AUTH_HEADERS,
        ).status_code
        == 200
    )


def test_sem_credencial_nenhuma_as_mesmas_rotas_respondem_401(api: TestClient) -> None:
    assert api.get("/api/relatorios/conferencia").status_code == 401
    assert api.get("/api/regras").status_code == 401
    assert api.get("/api/alertas").status_code == 401


# --- critério de aceite nº 1: auditoria com nome -------------------------------


def _logs_do_documento(sessao: Session, documento_id: uuid.UUID) -> list[LogConferencia]:
    sessao.expire_all()
    return list(
        sessao.scalars(
            select(LogConferencia)
            .where(LogConferencia.documento_id == documento_id)
            .order_by(LogConferencia.created_at)
        )
    )


def test_duas_pessoas_produzem_duas_linhas_de_log_com_usuario_distinto(
    clientes: dict[Papel, TestClient],
    usuarios: dict[Papel, Usuario],
    sessao: Session,
    cenario: Cenario,
) -> None:
    """Critério de aceite nº 1 da issue #30, o que a auditoria não conseguia responder antes."""
    primeira, segunda = cenario.pendencias
    assert (
        clientes[Papel.CONFERENTE]
        .patch(f"/api/pendencias/{primeira.id}", json={"status": "em_correcao"})
        .status_code
        == 200
    )
    assert (
        clientes[Papel.COORDENADOR]
        .patch(f"/api/pendencias/{segunda.id}", json={"status": "em_correcao"})
        .status_code
        == 200
    )

    (log_conferente,) = _logs_do_documento(sessao, cenario.documentos[0].id)
    (log_coordenador,) = _logs_do_documento(sessao, cenario.documentos[1].id)

    assert log_conferente.acao == "transicao:problema->em_correcao"
    assert log_coordenador.acao == "transicao:problema->em_correcao"
    assert log_conferente.usuario != log_coordenador.usuario
    assert log_conferente.usuario == usuarios[Papel.CONFERENTE].email
    assert log_coordenador.usuario == usuarios[Papel.COORDENADOR].email
    assert log_conferente.usuario_id == usuarios[Papel.CONFERENTE].id
    assert log_coordenador.usuario_id == usuarios[Papel.COORDENADOR].id


def test_acao_por_chave_de_api_registra_api_e_usuario_id_nulo(
    api: TestClient, sessao: Session, cenario: Cenario
) -> None:
    """Não há pessoa por trás da chave: forjar um id faria a auditoria apontar
    para alguém que não fez nada."""
    resposta = api.patch(
        f"/api/pendencias/{cenario.pendencias[0].id}",
        json={"status": "em_correcao"},
        headers=AUTH_HEADERS,
    )

    assert resposta.status_code == 200
    (log,) = _logs_do_documento(sessao, cenario.documentos[0].id)
    assert log.usuario == "api"
    assert log.usuario_id is None


# --- atribuição de pendência a uma pessoa cadastrada ---------------------------


def test_patch_atribui_a_pendencia_a_um_usuario_cadastrado(
    clientes: dict[Papel, TestClient],
    usuarios: dict[Papel, Usuario],
    sessao: Session,
    cenario: Cenario,
) -> None:
    responsavel = usuarios[Papel.CONFERENTE]

    resposta = clientes[Papel.COORDENADOR].patch(
        f"/api/pendencias/{cenario.pendencias[0].id}",
        json={"status": "em_correcao", "responsavel_id": str(responsavel.id)},
    )

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["responsavel_id"] == str(responsavel.id)
    # `responsavel` guarda o nome como instantâneo legível.
    assert corpo["responsavel"] == responsavel.nome


def test_patch_com_responsavel_id_inexistente_responde_422(
    clientes: dict[Papel, TestClient], cenario: Cenario, sessao: Session
) -> None:
    resposta = clientes[Papel.COORDENADOR].patch(
        f"/api/pendencias/{cenario.pendencias[0].id}",
        json={"status": "em_correcao", "responsavel_id": str(uuid.uuid4())},
    )

    assert resposta.status_code == 422
    # E a transição não aconteceu: um campo errado do corpo não pode deixar a
    # pendência meio transicionada.
    sessao.expire_all()
    pendencia = sessao.get(Pendencia, cenario.pendencias[0].id)
    assert pendencia is not None
    assert pendencia.status is PendenciaStatus.ABERTA


def test_pendencia_aberta_pela_classificacao_nasce_sem_responsavel_id(
    clientes: dict[Papel, TestClient], cenario: Cenario
) -> None:
    """Pendência automática não tem pessoa, e inventar uma seria mentir sobre
    quem está cobrando o quê."""
    resposta = clientes[Papel.CONFERENTE].get(
        f"/api/pendencias?operadora_id={cenario.operadora.id}"
    )

    assert resposta.status_code == 200
    for item in resposta.json()["data"]:
        assert item["responsavel_id"] is None
