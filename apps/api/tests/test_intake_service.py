"""Testes do serviço de intake. Nenhum toca Postgres, MinIO nem a API do modelo.

Os dublês vivem em `tests/fakes.py`. O repositório em memória reproduz a
unicidade de `documentos.idempotency_key` levantando `IntegrityError`, que é o
único sinal de colisão que o serviço observa.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from homecareos.db.models import Documento, DocumentoStatus, TipoDocumento
from homecareos.intake.errors import (
    IdempotencyConflictError,
    InvalidDocumentError,
    UnsupportedMediaTypeError,
)
from homecareos.intake.pdf import PageImage
from homecareos.intake.repository import DocumentoRegistrado
from homecareos.intake.service import (
    ACAO_EXTRACAO_FALHOU,
    USUARIO_SISTEMA,
    ResultadoUpload,
    receber_upload,
)
from homecareos.storage import StorageError, build_key
from tests.fakes import (
    FailingDispatcher,
    FailingStorage,
    FakeDispatcher,
    FakeDocumentoRepository,
    FakeStorage,
    make_pdf,
    make_png,
)

COMPETENCIA = "2024-03"


def _receber(
    conteudo: bytes,
    *,
    repository: FakeDocumentoRepository,
    storage: FakeStorage | FailingStorage,
    dispatcher: FakeDispatcher | FailingDispatcher,
    idempotency_key: str | None = None,
    filename: str = "evolucao.pdf",
    paciente_id: uuid.UUID | None = None,
    operadora_id: uuid.UUID | None = None,
    usuario: str = USUARIO_SISTEMA,
    usuario_id: uuid.UUID | None = None,
) -> ResultadoUpload:
    return receber_upload(
        conteudo=conteudo,
        filename=filename,
        competencia=COMPETENCIA,
        idempotency_key=idempotency_key,
        repository=repository,
        storage=storage,
        dispatcher=dispatcher,
        paciente_id=paciente_id,
        operadora_id=operadora_id,
        usuario=usuario,
        usuario_id=usuario_id,
    )


# --- AC1/AC2/AC3: um documento por página, no storage e na extração -----------


def test_pdf_de_dez_paginas_cria_dez_documentos_em_processando() -> None:
    repository, storage, dispatcher = (
        FakeDocumentoRepository(),
        FakeStorage(),
        FakeDispatcher(),
    )

    resultado = _receber(
        make_pdf(10), repository=repository, storage=storage, dispatcher=dispatcher
    )

    assert resultado.ja_existia is False
    assert len(resultado.documentos) == 10
    assert [documento.pagina for documento in resultado.documentos] == list(range(1, 11))
    assert all(
        documento.status is DocumentoStatus.PROCESSANDO for documento in resultado.documentos
    )
    assert all(documento.competencia == COMPETENCIA for documento in resultado.documentos)


def test_cada_pagina_vai_ao_storage_sob_a_chave_de_build_key() -> None:
    """O sha256 da chave é do conteúdo da página renderizada, não do PDF original."""
    repository, storage, dispatcher = (
        FakeDocumentoRepository(),
        FakeStorage(),
        FakeDispatcher(),
    )
    conteudo = make_pdf(10)

    resultado = _receber(conteudo, repository=repository, storage=storage, dispatcher=dispatcher)

    assert len(storage.objetos) == 10
    sha_do_arquivo_original = hashlib.sha256(conteudo).hexdigest()
    for documento, chave in zip(resultado.documentos, storage.chaves, strict=True):
        dados, content_type = storage.objetos[chave]
        assert content_type == "image/png"
        assert chave == build_key(documento.id, hashlib.sha256(dados).hexdigest(), ".png")
        assert sha_do_arquivo_original not in chave


def test_extracao_e_disparada_uma_vez_por_pagina_com_o_documento_id_real() -> None:
    repository, storage, dispatcher = (
        FakeDocumentoRepository(),
        FakeStorage(),
        FakeDispatcher(),
    )

    resultado = _receber(
        make_pdf(10), repository=repository, storage=storage, dispatcher=dispatcher
    )

    assert dispatcher.chamadas == [
        (documento.id, documento.pagina) for documento in resultado.documentos
    ]


def test_png_cria_um_unico_documento_com_extensao_png() -> None:
    repository, storage, dispatcher = (
        FakeDocumentoRepository(),
        FakeStorage(),
        FakeDispatcher(),
    )

    resultado = _receber(
        make_png(),
        repository=repository,
        storage=storage,
        dispatcher=dispatcher,
        filename="foto.png",
    )

    assert len(resultado.documentos) == 1
    assert storage.chaves[0].endswith(".png")


# --- AC4/AC5: idempotência decidida pelo banco --------------------------------


def test_chave_de_idempotencia_e_derivada_por_pagina() -> None:
    """Um upload cria N documentos e a coluna é única: os N não podem
    compartilhar a mesma chave."""
    repository, storage, dispatcher = (
        FakeDocumentoRepository(),
        FakeStorage(),
        FakeDispatcher(),
    )

    _receber(
        make_pdf(3),
        repository=repository,
        storage=storage,
        dispatcher=dispatcher,
        idempotency_key="chave-do-cliente",
    )

    assert set(repository.por_chave) == {
        "chave-do-cliente:1",
        "chave-do-cliente:2",
        "chave-do-cliente:3",
    }


def test_reenvio_com_a_mesma_chave_devolve_os_mesmos_documentos_sem_reextrair() -> None:
    """A extração custa dinheiro por chamada: reenvio não pode chamá-la de novo."""
    repository, storage, dispatcher = (
        FakeDocumentoRepository(),
        FakeStorage(),
        FakeDispatcher(),
    )
    conteudo = make_pdf(3)

    primeiro = _receber(
        conteudo,
        repository=repository,
        storage=storage,
        dispatcher=dispatcher,
        idempotency_key="chave-do-cliente",
    )
    chamadas_apos_primeiro = len(dispatcher.chamadas)
    documentos_apos_primeiro = len(repository.documentos)

    segundo = _receber(
        conteudo,
        repository=repository,
        storage=storage,
        dispatcher=dispatcher,
        idempotency_key="chave-do-cliente",
    )

    assert segundo.ja_existia is True
    assert [d.id for d in segundo.documentos] == [d.id for d in primeiro.documentos]
    assert len(repository.documentos) == documentos_apos_primeiro
    assert len(dispatcher.chamadas) == chamadas_apos_primeiro
    # Zero rollbacks: o reenvio agora é resolvido antes do INSERT, e portanto
    # antes do IntegrityError. O caminho do rollback continua coberto pelo
    # teste do reenvio concorrente, onde a consulta prévia não vê nada e só o
    # índice único decide.
    assert repository.rollbacks == 0


def test_reenvio_sem_chave_cria_documentos_novos() -> None:
    """Sem `Idempotency-Key` não há promessa de deduplicação."""
    repository, storage, dispatcher = (
        FakeDocumentoRepository(),
        FakeStorage(),
        FakeDispatcher(),
    )
    conteudo = make_pdf(2)

    primeiro = _receber(conteudo, repository=repository, storage=storage, dispatcher=dispatcher)
    segundo = _receber(conteudo, repository=repository, storage=storage, dispatcher=dispatcher)

    assert segundo.ja_existia is False
    assert {d.id for d in primeiro.documentos}.isdisjoint({d.id for d in segundo.documentos})
    assert len(repository.documentos) == 4
    assert len(dispatcher.chamadas) == 4


def test_colisao_parcial_de_chave_vira_conflito_explicito() -> None:
    """Mesma chave para um arquivo com outro número de páginas: a chave prometia
    a mesma requisição, e devolver o upload anterior esconderia a troca."""
    repository, storage, dispatcher = (
        FakeDocumentoRepository(),
        FakeStorage(),
        FakeDispatcher(),
    )
    _receber(
        make_pdf(3),
        repository=repository,
        storage=storage,
        dispatcher=dispatcher,
        idempotency_key="chave-do-cliente",
    )

    with pytest.raises(IdempotencyConflictError):
        _receber(
            make_pdf(5),
            repository=repository,
            storage=storage,
            dispatcher=dispatcher,
            idempotency_key="chave-do-cliente",
        )


def test_integrity_error_que_nao_e_de_idempotencia_sobe() -> None:
    """Violação de integridade sem chave colidente não é reenvio — não pode ser
    silenciada como 200."""

    class RepositorioQueSempreViola(FakeDocumentoRepository):
        def criar_documentos(self, documentos: list[Documento]) -> list[DocumentoRegistrado]:
            raise IntegrityError("INSERT INTO documentos ...", {}, Exception("fk violation"))

    repository = RepositorioQueSempreViola()

    with pytest.raises(IntegrityError):
        _receber(
            make_pdf(1),
            repository=repository,
            storage=FakeStorage(),
            dispatcher=FakeDispatcher(),
            idempotency_key="chave-do-cliente",
        )


# --- AC6: falha de extração não pode derrubar o upload ------------------------


def test_falha_de_extracao_nao_desfaz_o_upload_e_vira_log_de_conferencia() -> None:
    repository, storage = FakeDocumentoRepository(), FakeStorage()
    dispatcher = FailingDispatcher()

    resultado = _receber(make_pdf(3), repository=repository, storage=storage, dispatcher=dispatcher)

    assert len(resultado.documentos) == 3
    assert all(
        documento.status is DocumentoStatus.PROCESSANDO for documento in resultado.documentos
    )
    assert len(repository.logs) == 3
    assert {log["acao"] for log in repository.logs} == {ACAO_EXTRACAO_FALHOU}
    assert {log["documento_id"] for log in repository.logs} == {
        str(documento.id) for documento in resultado.documentos
    }
    assert all("RuntimeError" in log["detalhe"] for log in repository.logs)


def test_falha_de_extracao_em_uma_pagina_nao_impede_as_outras() -> None:
    repository, storage = FakeDocumentoRepository(), FakeStorage()

    class SoAPrimeiraFalha(FakeDispatcher):
        def dispatch(
            self,
            documento_id: uuid.UUID,
            pagina: PageImage,
            *,
            usuario: str = "sistema",
            usuario_id: uuid.UUID | None = None,
        ) -> None:
            if pagina.numero == 1:
                raise RuntimeError("provider caiu na primeira página")
            super().dispatch(documento_id, pagina, usuario=usuario, usuario_id=usuario_id)

    dispatcher = SoAPrimeiraFalha()

    resultado = _receber(make_pdf(3), repository=repository, storage=storage, dispatcher=dispatcher)

    assert len(resultado.documentos) == 3
    assert [pagina for _id, pagina in dispatcher.chamadas] == [2, 3]
    assert len(repository.logs) == 1


# --- Erros de entrada e de infraestrutura ------------------------------------


def test_arquivo_nao_suportado_nao_grava_nada() -> None:
    repository, storage, dispatcher = (
        FakeDocumentoRepository(),
        FakeStorage(),
        FakeDispatcher(),
    )

    with pytest.raises(UnsupportedMediaTypeError):
        _receber(
            b"conteudo texto puro",
            repository=repository,
            storage=storage,
            dispatcher=dispatcher,
            filename="evolucao.txt",
        )

    assert repository.documentos == {}
    assert storage.objetos == {}


def test_pdf_corrompido_vira_invalid_document_error() -> None:
    repository, storage, dispatcher = (
        FakeDocumentoRepository(),
        FakeStorage(),
        FakeDispatcher(),
    )

    with pytest.raises(InvalidDocumentError):
        _receber(
            b"%PDF-1.4 lixo",
            repository=repository,
            storage=storage,
            dispatcher=dispatcher,
        )


def test_storage_indisponivel_nao_cria_documento() -> None:
    repository, dispatcher = FakeDocumentoRepository(), FakeDispatcher()

    with pytest.raises(StorageError):
        _receber(
            make_pdf(2),
            repository=repository,
            storage=FailingStorage(),
            dispatcher=dispatcher,
        )

    assert repository.documentos == {}
    assert dispatcher.chamadas == []


def test_tipo_padrao_do_documento_e_evolucao() -> None:
    """É a evolução que o técnico envia para comprovar a visita; a
    classificação automática do tipo é outro módulo."""
    tipos: list[TipoDocumento] = []

    class CapturaTipo(FakeDocumentoRepository):
        def criar_documentos(self, documentos: list[Documento]) -> list[DocumentoRegistrado]:
            tipos.extend(documento.tipo for documento in documentos)
            return super().criar_documentos(documentos)

    _receber(
        make_pdf(1),
        repository=CapturaTipo(),
        storage=FakeStorage(),
        dispatcher=FakeDispatcher(),
    )

    assert tipos == [TipoDocumento.EVOLUCAO]


def test_reenvio_nao_grava_objeto_orfao_no_storage() -> None:
    """O replay sai antes do storage: cópia não referenciada de documento
    clínico acumulando no bucket é exatamente o que não se quer."""
    storage = FakeStorage()
    repository = FakeDocumentoRepository()
    dispatcher = FakeDispatcher()
    argumentos = {
        "conteudo": make_pdf(3),
        "filename": "evolucao.pdf",
        "competencia": "2024-03",
        "idempotency_key": "chave-fixa",
        "repository": repository,
        "storage": storage,
        "dispatcher": dispatcher,
    }

    receber_upload(**argumentos)
    objetos_apos_primeiro = len(storage.objetos)

    resultado = receber_upload(**argumentos)

    assert resultado.ja_existia is True
    assert len(storage.objetos) == objetos_apos_primeiro  # nenhum objeto novo
    assert len(dispatcher.chamadas) == 3  # extração não roda de novo


def test_reenvio_concorrente_ainda_e_resolvido_pelo_indice_unico() -> None:
    """O curto-circuito é otimização, não a regra.

    Simula dois uploads simultâneos: a consulta prévia não enxerga nada (o
    outro ainda não commitou) e o INSERT estoura. Quem decide continua sendo o
    índice único do Postgres, não a consulta.
    """
    repository = FakeDocumentoRepository()
    storage = FakeStorage()
    dispatcher = FakeDispatcher()
    primeiro = _receber(
        conteudo=make_pdf(2),
        repository=repository,
        storage=storage,
        dispatcher=dispatcher,
        idempotency_key="chave-concorrente",
    )

    # O segundo upload não vê o primeiro na consulta prévia, mas colide no INSERT.
    visivel = dict(repository.por_chave)
    repository.por_chave.clear()
    chamadas_antes = len(repository.documentos)

    def _restaurar_ao_falhar(_documentos: object) -> None:
        repository.por_chave.update(visivel)
        raise IntegrityError("INSERT INTO documentos ...", {}, Exception("duplicate key"))

    monkey = repository.criar_documentos
    repository.criar_documentos = _restaurar_ao_falhar  # type: ignore[method-assign]
    try:
        segundo = _receber(
            conteudo=make_pdf(2),
            repository=repository,
            storage=storage,
            dispatcher=dispatcher,
            idempotency_key="chave-concorrente",
        )
    finally:
        repository.criar_documentos = monkey  # type: ignore[method-assign]

    assert segundo.ja_existia is True
    assert [d.id for d in segundo.documentos] == [d.id for d in primeiro.documentos]
    assert repository.rollbacks == 1
    assert len(repository.documentos) == chamadas_antes


def test_upload_associa_paciente_e_operadora() -> None:
    """Sem operadora no documento o motor de regras não roda.

    A operadora é quem determina quais regras de glosa se aplicam; documento
    sem ela atravessa a conferência sem ser conferido.
    """
    paciente = uuid.uuid4()
    operadora = uuid.uuid4()
    repository = FakeDocumentoRepository()

    resultado = _receber(
        conteudo=make_pdf(2),
        repository=repository,
        storage=FakeStorage(),
        dispatcher=FakeDispatcher(),
        paciente_id=paciente,
        operadora_id=operadora,
    )

    assert len(resultado.documentos) == 2
    assert all(d.paciente_id == paciente for d in repository.criados)
    assert all(d.operadora_id == operadora for d in repository.criados)


# --- issue #30: identidade real na auditoria do upload -------------------------


def test_usuario_e_usuario_id_sao_repassados_ao_dispatcher() -> None:
    """Quem fez o upload chega até o dispatcher — dados simples, não o `Principal`."""
    repository, storage, dispatcher = (
        FakeDocumentoRepository(),
        FakeStorage(),
        FakeDispatcher(),
    )
    usuario_id = uuid.uuid4()

    resultado = _receber(
        make_pdf(2),
        repository=repository,
        storage=storage,
        dispatcher=dispatcher,
        usuario="ana@exemplo.com",
        usuario_id=usuario_id,
    )

    assert len(resultado.documentos) == 2
    assert dispatcher.autores == [("ana@exemplo.com", usuario_id)] * 2


def test_sem_usuario_o_default_e_sistema_com_usuario_id_nulo() -> None:
    """Sem autor (cron, script, chamada antiga), o comportamento não muda."""
    repository, storage, dispatcher = (
        FakeDocumentoRepository(),
        FakeStorage(),
        FakeDispatcher(),
    )

    _receber(make_pdf(1), repository=repository, storage=storage, dispatcher=dispatcher)

    assert dispatcher.autores == [(USUARIO_SISTEMA, None)]
