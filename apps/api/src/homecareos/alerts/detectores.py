"""Os quatro detectores de alerta da issue #9.

Todos têm a mesma assinatura — `detectar_X(session, settings) -> list[Alerta]` —
e todos são **consultas agregadas**: nenhum abre laço com consulta dentro. A
varredura roda no cron sobre a base inteira; um detector que consultasse por
documento transformaria um alerta em algumas centenas de round-trips.

Nenhum detector envia nada nem escreve nada. Eles respondem "o que está errado
agora?"; quem decide se isso vira mensagem (e para quem, e se já não foi
avisado) é `alerts/service.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session

from homecareos.alerts.schema import Alerta, TipoAlerta
from homecareos.config import Settings
from homecareos.db.models import (
    Documento,
    DocumentoStatus,
    Operadora,
    Paciente,
    Pendencia,
    PendenciaStatus,
)

# Os campos cuja ausência manda a evolução de volta para o campo com urgência.
# É a leitura literal da issue #9 ("evolução sem carimbo ou sem assinatura —
# precisa voltar pro campo urgente"): é REGRA DE NEGÓCIO, não filtro de
# consulta, e é por isso que ela é uma constante nomeada aqui em vez de estar
# embutida no `where`. Acrescentar um campo a este conjunto é decidir que mais
# um tipo de falha merece acordar alguém no WhatsApp.
CAMPOS_CRITICOS = frozenset(
    {"carimbo_presente", "carimbo_legivel", "assinatura_profissional_presente"}
)

ACAO_DOCUMENTO_CRITICO = "Reenviar a evolução com carimbo e assinatura."
SEM_OPERADORA = "sem-operadora"
VALOR_AUSENTE = "não informado"

_SEPARADOR_PROBLEMAS = " | "


def _texto(valor: str | None) -> str:
    """`None` e string vazia viram `"não informado"`, nunca `"None"` na mensagem."""
    return valor if valor else VALOR_AUSENTE


def _data(momento: datetime | None) -> str:
    return momento.strftime("%d/%m/%Y") if momento is not None else VALOR_AUSENTE


def _percentual(taxa: float) -> str:
    return f"{taxa * 100:.1f}%"


def detectar_documento_incompleto_critico(
    session: Session, settings: Settings, *, documento_id: uuid.UUID | None = None
) -> list[Alerta]:
    """Documento `incompleto` com pendência não resolvida em campo crítico.

    `documento_id` restringe a detecção a um único documento: é o que o gancho
    da classificação (`alerts/hooks.py`) usa para avisar na hora, sem varrer a
    base inteira a cada upload.
    """
    stmt = (
        select(
            Documento.id,
            Paciente.nome,
            Operadora.nome,
            func.string_agg(
                Pendencia.descricao,
                aggregate_order_by(literal(_SEPARADOR_PROBLEMAS), Pendencia.created_at),
            ),
            func.min(Pendencia.deadline),
        )
        .join(Pendencia, Pendencia.documento_id == Documento.id)
        .outerjoin(Paciente, Paciente.id == Documento.paciente_id)
        .outerjoin(Operadora, Operadora.id == Documento.operadora_id)
        .where(
            Documento.status == DocumentoStatus.INCOMPLETO,
            Pendencia.status != PendenciaStatus.RESOLVIDA,
            Pendencia.campo.in_(CAMPOS_CRITICOS),
        )
        .group_by(Documento.id, Paciente.nome, Operadora.nome)
    )
    if documento_id is not None:
        stmt = stmt.where(Documento.id == documento_id)

    return [
        Alerta(
            tipo=TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO,
            chave=f"documento:{doc_id}",
            documento_id=doc_id,
            contexto={
                "paciente": _texto(paciente),
                "operadora": _texto(operadora),
                "problema": _texto(problema),
                "deadline": _data(deadline),
                "acao": ACAO_DOCUMENTO_CRITICO,
            },
        )
        for doc_id, paciente, operadora, problema, deadline in session.execute(stmt).all()
    ]


def detectar_deadline_competencia(session: Session, settings: Settings) -> list[Alerta]:
    """Competências cujo prazo de envio está chegando com pendência ainda em aberto.

    Agrupa por `(competencia, operadora_id)` porque é assim que o faturamento
    sai: um lote por operadora por competência. Um alerta por documento aqui
    seria o oposto do que a issue pede — no fechamento são dezenas deles, e o
    aviso útil é "a Unimed tem 12 documentos pendentes e o envio é sexta".
    """
    agora = datetime.now(UTC)
    limite = agora + timedelta(days=settings.alertas_dias_antes_deadline)

    documentos_distintos = func.count(func.distinct(Documento.id))
    stmt = (
        select(
            Documento.competencia,
            Documento.operadora_id,
            Operadora.nome,
            documentos_distintos,
            func.min(Pendencia.deadline),
        )
        .join(Pendencia, Pendencia.documento_id == Documento.id)
        .outerjoin(Operadora, Operadora.id == Documento.operadora_id)
        .where(
            Pendencia.status != PendenciaStatus.RESOLVIDA,
            Pendencia.deadline >= agora,
            Pendencia.deadline <= limite,
        )
        .group_by(Documento.competencia, Documento.operadora_id, Operadora.nome)
    )

    alertas: list[Alerta] = []
    for competencia, operadora_id, operadora_nome, documentos, menor_deadline in session.execute(
        stmt
    ).all():
        # O `join` garante contagem > 0 em todo grupo que sobrevive ao `where`;
        # a guarda existe para o critério ("só emite para grupo com contagem
        # > 0") continuar explícito se o `join` virar `outerjoin` um dia.
        if documentos <= 0:
            continue
        # Dias inteiros até o prazo, nunca negativo: `menor_deadline >= agora`
        # pelo `where`, mas arredondar para baixo um prazo de poucas horas
        # produz 0 — e "faltam 0 dia(s)" é a leitura correta de "é hoje".
        dias = max((menor_deadline - agora).days, 0)
        alertas.append(
            Alerta(
                tipo=TipoAlerta.DEADLINE_COMPETENCIA,
                chave=f"competencia:{competencia}:{operadora_id or SEM_OPERADORA}",
                contexto={
                    "operadora": _texto(operadora_nome),
                    "competencia": competencia,
                    "documentos": str(documentos),
                    "dias": str(dias),
                    "deadline": _data(menor_deadline),
                },
            )
        )
    return alertas


def detectar_volume_anormal(session: Session, settings: Settings) -> list[Alerta]:
    """Taxa de problema de hoje muito acima da média recente — cheiro de erro sistêmico.

    Dispara só quando as **três** condições valem: volume de hoje acima do piso,
    janela de referência não vazia, e taxa de hoje acima da média vezes o fator.

    O piso e a exigência de janela não vazia são o ponto do detector, não
    filigrana: sem eles, **um** documento com problema num dia parado dá 100% de
    taxa e dispara alerta todo dia. Esse é o jeito mais rápido de ensinar a
    equipe a ignorar o WhatsApp — e um alerta ignorado é pior que alerta
    nenhum, porque dá a sensação de que o sistema está avisando.
    """
    hoje = datetime.now(UTC).date()
    inicio_hoje = datetime.combine(hoje, time.min, tzinfo=UTC)
    fim_hoje = inicio_hoje + timedelta(days=1)
    inicio_janela = inicio_hoje - timedelta(days=settings.alertas_volume_janela_dias)

    tem_pendencia = select(Pendencia.id).where(Pendencia.documento_id == Documento.id).exists()
    eh_hoje = Documento.created_at >= inicio_hoje

    # Uma consulta só, com agregação condicional: hoje e a janela de referência
    # saem da mesma varredura da tabela.
    linha = session.execute(
        select(
            func.count().filter(eh_hoje),
            func.count().filter(eh_hoje, tem_pendencia),
            func.count().filter(~eh_hoje),
            func.count().filter(~eh_hoje, tem_pendencia),
        )
        .select_from(Documento)
        .where(Documento.created_at >= inicio_janela, Documento.created_at < fim_hoje)
    ).one()
    documentos_hoje, com_pendencia_hoje, documentos_janela, com_pendencia_janela = linha

    if documentos_hoje < settings.alertas_volume_minimo_documentos:
        return []
    if documentos_janela <= 0:
        return []

    taxa_hoje = com_pendencia_hoje / documentos_hoje
    taxa_media = com_pendencia_janela / documentos_janela
    if taxa_hoje <= taxa_media * settings.alertas_volume_fator:
        return []

    return [
        Alerta(
            tipo=TipoAlerta.VOLUME_ANORMAL,
            chave=f"volume:{hoje.isoformat()}",
            contexto={
                "data": hoje.strftime("%d/%m/%Y"),
                "documentos": str(documentos_hoje),
                "taxa_hoje": _percentual(taxa_hoje),
                "janela": str(settings.alertas_volume_janela_dias),
                "taxa_media": _percentual(taxa_media),
            },
        )
    ]


def detectar_pendencia_parada(session: Session, settings: Settings) -> list[Alerta]:
    """Pendência `aberta` há mais que o limite configurado, sem ninguém tocar nela.

    Só `aberta`, deliberadamente: `em_correcao` significa que alguém já pegou o
    problema para si, e cobrar quem está trabalhando é exatamente o alerta que a
    equipe aprende a silenciar.
    """
    agora = datetime.now(UTC)
    limite = agora - timedelta(hours=settings.alertas_horas_pendencia_parada)

    stmt = (
        select(
            Pendencia.id,
            Pendencia.descricao,
            Pendencia.created_at,
            Pendencia.deadline,
            Documento.id,
            Paciente.nome,
            Operadora.nome,
        )
        .join(Documento, Documento.id == Pendencia.documento_id)
        .outerjoin(Paciente, Paciente.id == Documento.paciente_id)
        .outerjoin(Operadora, Operadora.id == Documento.operadora_id)
        .where(Pendencia.status == PendenciaStatus.ABERTA, Pendencia.created_at < limite)
    )

    return [
        Alerta(
            tipo=TipoAlerta.PENDENCIA_PARADA,
            chave=f"pendencia:{pendencia_id}",
            documento_id=doc_id,
            contexto={
                "paciente": _texto(paciente),
                "operadora": _texto(operadora),
                "problema": _texto(descricao),
                "horas": str(int((agora - criada_em).total_seconds() // 3600)),
                "deadline": _data(deadline),
            },
        )
        for (
            pendencia_id,
            descricao,
            criada_em,
            deadline,
            doc_id,
            paciente,
            operadora,
        ) in session.execute(stmt).all()
    ]


def detectar_todos(session: Session, settings: Settings) -> list[Alerta]:
    """Roda os quatro detectores, na ordem em que a issue os lista."""
    return [
        *detectar_documento_incompleto_critico(session, settings),
        *detectar_deadline_competencia(session, settings),
        *detectar_volume_anormal(session, settings),
        *detectar_pendencia_parada(session, settings),
    ]
