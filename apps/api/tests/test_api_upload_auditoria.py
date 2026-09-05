"""`POST /api/documentos` grava em `log_conferencia` quem fez o upload — issue #30.

Antes desta trilha, `extraction/dispatcher.py` chamava `classificar_documento`
com `usuario="sistema"` fixo, então todo upload — mesmo autenticado — perdia a
identidade de quem enviou no momento em que a classificação automática grava a
transição em `log_conferencia`. Este arquivo trava o critério de aceite
literal da issue #30 para o caminho do upload, do mesmo jeito que
`test_autorizacao_papeis.py` já trava para a transição de pendência.

`storage` é um dublê em memória (nenhum objeto precisa ir a um MinIO de
verdade para provar quem gravou a auditoria) e o `dispatcher` é o
`SyncExtractionDispatcher` real, com `session_factory` apontando para o
mesmo Postgres do restante da suíte de integração — é a peça sob teste, e
trocá-la por um dublê esconderia exatamente o bug que a issue #30 descreve.
O provider de extração é um dublê determinístico que sempre reprova
`carimbo_legivel`, para garantir que a classificação transicione o documento
e grave a linha de `log_conferencia` que os testes leem.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from homecareos.auth import senhas
from homecareos.auth.schema import Papel
from homecareos.config import Settings, get_settings
from homecareos.db.models import LogConferencia, Operadora, Regra, Usuario
from homecareos.db.session import get_sessionmaker
from homecareos.extraction.dispatcher import SyncExtractionDispatcher
from homecareos.extraction.schema import EvolucaoProntuario, ExtractionResult, PaginaDocumento
from homecareos.intake.router import get_document_storage, get_extraction_dispatcher
from homecareos.main import app
from tests.conftest import AUTH_HEADERS, TEST_API_KEY, TEST_API_KEY_PAPEIS
from tests.fakes import FakeStorage, make_pdf

pytestmark = pytest.mark.integration

SONDA_TIMEOUT = 2
SENHA_DE_TESTE = "senha-de-teste-upload-auditoria"
COMPETENCIA_TESTE = "2099-08"


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


@dataclass
class ProviderQueSempreReprova:
    """Devolve `carimbo_legivel=False`: garante que a regra reprove e o
    documento transicione — é a transição que grava a linha de `log_conferencia`
    sob teste (ver `classification/service.transicionar`)."""

    def extract(self, pagina: PaginaDocumento, documento_id: str | None = None) -> ExtractionResult:
        return ExtractionResult(
            campos=EvolucaoProntuario(carimbo_legivel=False),
            confianca=0.9,
            confianca_por_campo={},
            raw_response={},
            modelo="modelo-teste",
            provider="teste",
        )


@pytest.fixture(scope="module")
def settings() -> Settings:
    resolved = get_settings()
    motivo = _postgres_responde(resolved)
    if motivo is not None:
        pytest.skip(f"Postgres indisponível em {resolved.database_url}: {motivo}")
    return resolved


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def api(settings: Settings, storage: FakeStorage) -> Iterator[TestClient]:
    """Cliente com storage em memória e o dispatcher síncrono real, contra o
    mesmo Postgres da suíte de integração — só a chamada ao modelo de visão é
    trocada por um dublê determinístico."""
    dispatcher = SyncExtractionDispatcher(
        provider=ProviderQueSempreReprova(), session_factory=get_sessionmaker()
    )
    app.dependency_overrides[get_document_storage] = lambda: storage
    app.dependency_overrides[get_extraction_dispatcher] = lambda: dispatcher
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={
            "api_keys": TEST_API_KEY,
            "api_key_papeis": TEST_API_KEY_PAPEIS,
            "environment": "local",
        }
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
    """Um conferente e um coordenador — os dois papéis autorizados a enviar
    documento (ver a matriz do README); `gestor` responderia 403."""
    criados = {
        papel: Usuario(
            nome=f"Pessoa {papel.value}",
            email=f"{papel.value}-upload-{uuid.uuid4()}@teste.local",
            senha_hash=senhas.gerar_hash(SENHA_DE_TESTE),
            papel=papel.value,
        )
        for papel in (Papel.CONFERENTE, Papel.COORDENADOR)
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
    settings: Settings, storage: FakeStorage, usuarios: dict[Papel, Usuario]
) -> Iterator[dict[Papel, TestClient]]:
    """Um `TestClient` por papel, já logado — cada um com o cookie da própria sessão."""
    dispatcher = SyncExtractionDispatcher(
        provider=ProviderQueSempreReprova(), session_factory=get_sessionmaker()
    )
    app.dependency_overrides[get_document_storage] = lambda: storage
    app.dependency_overrides[get_extraction_dispatcher] = lambda: dispatcher
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={
            "api_keys": TEST_API_KEY,
            "api_key_papeis": TEST_API_KEY_PAPEIS,
            "environment": "local",
        }
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


@pytest.fixture
def operadora(sessao: Session) -> Iterator[Operadora]:
    operadora = Operadora(nome="Operadora Upload Auditoria", codigo=f"AUD-{uuid.uuid4()}")
    sessao.add(operadora)
    sessao.flush()
    regra = Regra(
        operadora_id=operadora.id,
        campo="carimbo_legivel",
        condicao='{"tipo": "verdadeiro"}',
        acao="rejeitar",
        motivo_glosa="Carimbo ilegível",
    )
    sessao.add(regra)
    sessao.commit()

    yield operadora

    sessao.execute(text("delete from regras where operadora_id = :id"), {"id": operadora.id})
    sessao.execute(text("delete from operadoras where id = :id"), {"id": operadora.id})
    sessao.commit()


@pytest.fixture
def limpeza(sessao: Session) -> Iterator[list[uuid.UUID]]:
    """Apaga documento, extração, validação e log criados pelo teste."""
    criados: list[uuid.UUID] = []
    yield criados
    if criados:
        for tabela in ("pendencias", "validacoes", "extracoes", "log_conferencia"):
            sessao.execute(
                text(f"delete from {tabela} where documento_id = any(:ids)"), {"ids": criados}
            )
        sessao.execute(text("delete from documentos where id = any(:ids)"), {"ids": criados})
        sessao.commit()


def _upload(cliente: TestClient, operadora_id: uuid.UUID, **extra: Any) -> Any:
    return cliente.post(
        "/api/documentos",
        files={"arquivo": ("evolucao.pdf", make_pdf(1), "application/pdf")},
        data={"competencia": COMPETENCIA_TESTE, "operadora_id": str(operadora_id)},
        **extra,
    )


def _log_da_classificacao(sessao: Session, documento_id: uuid.UUID) -> LogConferencia:
    sessao.expire_all()
    (log,) = list(
        sessao.scalars(select(LogConferencia).where(LogConferencia.documento_id == documento_id))
    )
    return log


# --- critério de aceite da issue #30, no caminho do upload ---------------------


def test_upload_autenticado_grava_log_com_email_e_usuario_id(
    clientes: dict[Papel, TestClient],
    usuarios: dict[Papel, Usuario],
    sessao: Session,
    operadora: Operadora,
    limpeza: list[uuid.UUID],
) -> None:
    resposta = _upload(clientes[Papel.CONFERENTE], operadora.id)
    assert resposta.status_code == 201, resposta.text
    (documento_id,) = [uuid.UUID(d["id"]) for d in resposta.json()["documentos"]]
    limpeza.append(documento_id)

    log = _log_da_classificacao(sessao, documento_id)

    assert log.usuario == usuarios[Papel.CONFERENTE].email
    assert log.usuario_id == usuarios[Papel.CONFERENTE].id


def test_duas_pessoas_produzem_duas_linhas_de_log_com_usuario_distinto(
    clientes: dict[Papel, TestClient],
    usuarios: dict[Papel, Usuario],
    sessao: Session,
    operadora: Operadora,
    limpeza: list[uuid.UUID],
) -> None:
    """Critério de aceite literal da issue #30, agora no caminho do upload."""
    resposta_conferente = _upload(clientes[Papel.CONFERENTE], operadora.id)
    resposta_coordenador = _upload(clientes[Papel.COORDENADOR], operadora.id)
    assert resposta_conferente.status_code == 201, resposta_conferente.text
    assert resposta_coordenador.status_code == 201, resposta_coordenador.text

    (doc_conferente,) = [uuid.UUID(d["id"]) for d in resposta_conferente.json()["documentos"]]
    (doc_coordenador,) = [uuid.UUID(d["id"]) for d in resposta_coordenador.json()["documentos"]]
    limpeza.extend([doc_conferente, doc_coordenador])

    log_conferente = _log_da_classificacao(sessao, doc_conferente)
    log_coordenador = _log_da_classificacao(sessao, doc_coordenador)

    assert log_conferente.usuario != log_coordenador.usuario
    assert log_conferente.usuario == usuarios[Papel.CONFERENTE].email
    assert log_coordenador.usuario == usuarios[Papel.COORDENADOR].email
    assert log_conferente.usuario_id == usuarios[Papel.CONFERENTE].id
    assert log_coordenador.usuario_id == usuarios[Papel.COORDENADOR].id


def test_upload_por_x_api_key_grava_log_com_api_e_usuario_id_nulo(
    api: TestClient, sessao: Session, operadora: Operadora, limpeza: list[uuid.UUID]
) -> None:
    """Compatibilidade deliberada: a integração máquina-a-máquina não vira pessoa."""
    resposta = _upload(api, operadora.id, headers=AUTH_HEADERS)
    assert resposta.status_code == 201, resposta.text
    (documento_id,) = [uuid.UUID(d["id"]) for d in resposta.json()["documentos"]]
    limpeza.append(documento_id)

    log = _log_da_classificacao(sessao, documento_id)

    assert log.usuario == "api"
    assert log.usuario_id is None
