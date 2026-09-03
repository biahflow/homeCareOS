"""Persistência das tabelas de propriedade do intake: `documentos` e `log_conferencia`.

O intake é dono destas duas tabelas e de mais nenhuma. A extração tem o seu
próprio repositório (`homecareos.extraction.repository`) e escreve só
`extracoes`; nenhum dos dois importa o repositório do outro. É essa separação
que permite trocar a extração síncrona por uma fila (ou por um serviço
separado) sem inventar uma saga: cada lado commita a sua própria transação.

`DocumentoRepository` é uma porta (Protocol) e não a implementação concreta
porque o serviço de intake precisa ser testável sem container — o teste injeta
um repositório em memória que levanta o mesmo `IntegrityError` que o Postgres
levantaria na colisão de `idempotency_key`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from homecareos.db.models import Documento, DocumentoStatus, LogConferencia, Paciente


@dataclass(frozen=True)
class DocumentoRegistrado:
    """Os dados de um documento já gravado, desacoplados da sessão que o gravou.

    Instância do ORM expira no `commit` (`expire_on_commit` padrão), e ler um
    atributo depois disso dispara um SELECT extra por documento. Como o
    chamador só precisa destes quatro campos, eles são capturados antes do
    commit e devolvidos como dado puro.
    """

    id: uuid.UUID
    pagina: int
    status: DocumentoStatus
    competencia: str


class DocumentoRepository(Protocol):
    """Porta de escrita das tabelas do intake."""

    def criar_documentos(self, documentos: Sequence[Documento]) -> list[DocumentoRegistrado]:
        """Grava e **commita** os documentos.

        Levanta `sqlalchemy.exc.IntegrityError` quando algum `idempotency_key`
        já existe — a colisão é decidida pelo índice único do banco, nunca por
        um SELECT prévio do chamador.
        """
        ...

    def desfazer(self) -> None:
        """Descarta a transação corrente (após uma falha de integridade)."""
        ...

    def buscar_por_idempotency_keys(self, chaves: Sequence[str]) -> list[DocumentoRegistrado]:
        """Documentos já existentes para essas chaves, ordenados por página."""
        ...

    def operadora_do_paciente(self, paciente_id: uuid.UUID) -> uuid.UUID | None:
        """Operadora do paciente, ou `None` se o paciente não existir."""
        ...

    def registrar_log(
        self, *, documento_id: uuid.UUID, acao: str, usuario: str, detalhe: str
    ) -> None:
        """Grava e **commita** uma linha de auditoria em `log_conferencia`."""
        ...


def _registrado(documento: Documento) -> DocumentoRegistrado:
    return DocumentoRegistrado(
        id=documento.id,
        # `pagina` é nullable no model (documento pode não vir de um PDF
        # paginado), mas todo documento criado pelo intake tem página.
        pagina=documento.pagina if documento.pagina is not None else 1,
        status=documento.status,
        competencia=documento.competencia,
    )


@dataclass
class SqlAlchemyDocumentoRepository:
    """Implementação em SQLAlchemy da porta de escrita do intake."""

    session: Session

    def criar_documentos(self, documentos: Sequence[Documento]) -> list[DocumentoRegistrado]:
        self.session.add_all(documentos)
        # `flush` atribui os IDs e dispara a violação de unicidade; os valores
        # são capturados antes do commit, que expira as instâncias.
        self.session.flush()
        registrados = [_registrado(documento) for documento in documentos]
        self.session.commit()
        return registrados

    def desfazer(self) -> None:
        self.session.rollback()

    def buscar_por_idempotency_keys(self, chaves: Sequence[str]) -> list[DocumentoRegistrado]:
        if not chaves:
            return []
        stmt = (
            select(Documento)
            .where(Documento.idempotency_key.in_(list(chaves)))
            .order_by(Documento.pagina)
        )
        return [_registrado(documento) for documento in self.session.execute(stmt).scalars()]

    def operadora_do_paciente(self, paciente_id: uuid.UUID) -> uuid.UUID | None:
        paciente = self.session.get(Paciente, paciente_id)
        return paciente.operadora_id if paciente is not None else None

    def registrar_log(
        self, *, documento_id: uuid.UUID, acao: str, usuario: str, detalhe: str
    ) -> None:
        self.session.add(
            LogConferencia(
                documento_id=documento_id,
                acao=acao,
                usuario=usuario,
                detalhe=detalhe,
            )
        )
        self.session.commit()
