"""Acesso a `alertas_enviados`: grava o log e responde as perguntas do anti-bombardeio.

Nenhuma função aqui commita. Quem decide o limite da transação é
`alerts/service.py`, que commita uma vez ao final da varredura — as linhas de
uma passada entram juntas ou não entram.

`registrar` faz `flush()` de propósito. O `sessionmaker` do projeto é
`autoflush=False` (ver `db/session.py`), então uma linha só `add`-ada seria
invisível para as consultas de cooldown e rate limit da MESMA varredura: com
`max_por_hora=1`, dois alertas para o mesmo destinatário na mesma passada
seriam ambos enviados, porque o segundo não enxergaria o primeiro. O `flush`
escreve dentro da transação (não commita) e é o que fecha esse buraco.

## As duas defesas contam sobre chaves DIFERENTES (ADR 0006)

- **cooldown** conta por `(tipo, chave, destinatario)`: dois canais são dois
  endereços, e faz sentido o mesmo aviso sair nos dois;
- **rate limit** conta por **pessoa** quando a pessoa é conhecida, e por
  endereço quando não é. Contar sempre por endereço faria o telefone e o
  e-mail da mesma pessoa virarem destinatários não relacionados — e o efeito
  não seria uma exceção, seria o teto de mensagens por hora **dobrar sem
  ninguém pedir**. O rate limit existe para proteger a pessoa, não o endereço.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from homecareos.alerts.schema import Canal, Destinatario, StatusAlerta, TipoAlerta
from homecareos.auth.schema import Papel
from homecareos.db.models import AlertaEnviado, Usuario


def registrar(
    session: Session,
    *,
    tipo: TipoAlerta,
    canal: Canal,
    chave: str,
    destinatario: str,
    usuario_id: uuid.UUID | None,
    mensagem: str,
    status: StatusAlerta,
    detalhe: str | None = None,
    documento_id: uuid.UUID | None = None,
) -> AlertaEnviado:
    """Enfileira a linha de auditoria e a torna visível para a própria varredura.

    `canal` e `usuario_id` são obrigatórios na chamada (`usuario_id` aceita
    `None`, mas tem de ser dito): são o que distingue duas linhas do mesmo
    aviso e o que relaciona duas linhas da mesma pessoa. Um default silencioso
    aqui reintroduziria exatamente o defeito que o ADR 0006 fechou.
    """
    linha = AlertaEnviado(
        tipo=tipo.value,
        canal=canal.value,
        chave=chave,
        destinatario=destinatario,
        usuario_id=usuario_id,
        mensagem=mensagem,
        status=status.value,
        detalhe=detalhe,
        documento_id=documento_id,
    )
    session.add(linha)
    session.flush()
    return linha


def existe_envio_recente(
    session: Session, *, tipo: TipoAlerta, chave: str, destinatario: str, desde: datetime
) -> bool:
    """Este destinatário já foi avisado deste mesmo assunto desde `desde`?

    Só conta linha `enviado`: uma falha ou uma supressão anterior não é aviso
    entregue, e tratá-la como tal deixaria o destinatário sem nunca saber do
    problema.
    """
    return (
        session.execute(
            select(AlertaEnviado.id)
            .where(
                AlertaEnviado.tipo == tipo.value,
                AlertaEnviado.chave == chave,
                AlertaEnviado.destinatario == destinatario,
                AlertaEnviado.status == StatusAlerta.ENVIADO.value,
                AlertaEnviado.created_at >= desde,
            )
            .limit(1)
        ).first()
        is not None
    )


def contar_envios_desde(session: Session, *, destinatario: Destinatario, desde: datetime) -> int:
    """Quantas mensagens **esta pessoa** recebeu de fato desde `desde` (só `enviado`).

    A chave da contagem é `usuario_id` quando o sistema sabe de quem é o
    endereço, e o próprio endereço quando não sabe (telefone avulso do `.env`,
    que não tem vínculo com pessoa nenhuma porque não há telefone em
    `usuarios`). Contar pela pessoa é o que impede o teto por hora de dobrar
    quando o segundo canal é ligado: duas linhas em endereços diferentes da
    mesma pessoa somam no mesmo teto, em vez de cada endereço ganhar o seu.

    A assimetria é declarada, não escondida: para o telefone avulso, o endereço
    é o melhor que o dado permite (ADR 0006).
    """
    if destinatario.usuario_id is not None:
        alvo = AlertaEnviado.usuario_id == destinatario.usuario_id
    else:
        alvo = AlertaEnviado.destinatario == destinatario.endereco
    return int(
        session.execute(
            select(func.count())
            .select_from(AlertaEnviado)
            .where(
                alvo,
                AlertaEnviado.status == StatusAlerta.ENVIADO.value,
                AlertaEnviado.created_at >= desde,
            )
        ).scalar_one()
    )


def usuarios_ativos_por_papel(session: Session, *, papeis: Sequence[Papel]) -> list[Destinatario]:
    """E-mail e id das contas **ativas** com algum destes papéis, ordenados por e-mail.

    Mora em `alerts/` e não em `auth/` de propósito. Não existe consulta pronta
    de "usuários por papel" no projeto — a única parecida
    (`_coordenadores_ativos_alem_de`, em `auth/usuarios_router.py`) é privada,
    devolve uma contagem e serve a outra pergunta ("sobra coordenador se eu
    desativar este?"). Criar uma consulta pública em `auth/` que nenhum fluxo
    de autenticação usa alargaria a superfície daquele módulo por conveniência
    de outro; a convenção do projeto é a leitura morar com quem consome, e é o
    que `alerts/detectores.py` já faz ao ler `Documento`, `Pendencia`,
    `Paciente` e `Operadora` direto.

    Papel sem nenhuma conta ativa devolve lista vazia. **Não é erro**: é uma
    operação que ainda não tem gestor, ou um papel que ninguém ocupa hoje, e
    derrubar a varredura por causa disso deixaria de enviar também os alertas
    dos papéis que existem.

    Conta **desativada não recebe**: desativar é o caminho de saída de alguém
    da operação (ver `db/models/usuario.py`), e continuar mandando pendência de
    paciente para quem saiu é vazamento de dado de saúde, não só ruído.
    """
    if not papeis:
        return []
    linhas = session.execute(
        select(Usuario.id, Usuario.email)
        .where(
            Usuario.papel.in_([papel.value for papel in papeis]),
            Usuario.ativo.is_(True),
        )
        # Ordem estável: a mensagem de erro, o log e o teste ficam previsíveis.
        .order_by(Usuario.email)
    ).all()
    return [Destinatario(endereco=email, usuario_id=usuario_id) for usuario_id, email in linhas]


def listar(
    session: Session,
    *,
    tipo: TipoAlerta | None = None,
    status: StatusAlerta | None = None,
    documento_id: uuid.UUID | None = None,
    limite: int,
    offset: int,
) -> tuple[list[AlertaEnviado], int]:
    """Página do log e o total do filtro, do mais recente para o mais antigo."""
    stmt = select(AlertaEnviado)
    contagem = select(func.count()).select_from(AlertaEnviado)
    if tipo is not None:
        stmt = stmt.where(AlertaEnviado.tipo == tipo.value)
        contagem = contagem.where(AlertaEnviado.tipo == tipo.value)
    if status is not None:
        stmt = stmt.where(AlertaEnviado.status == status.value)
        contagem = contagem.where(AlertaEnviado.status == status.value)
    if documento_id is not None:
        stmt = stmt.where(AlertaEnviado.documento_id == documento_id)
        contagem = contagem.where(AlertaEnviado.documento_id == documento_id)

    total = int(session.execute(contagem).scalar_one())
    linhas = list(
        session.scalars(
            stmt.order_by(AlertaEnviado.created_at.desc()).limit(limite).offset(offset)
        ).all()
    )
    return linhas, total


def limpar_alertas_antigos(
    session: Session, *, antes_de: datetime, lote: int = 1000, dry_run: bool = False
) -> int:
    """Apaga linhas de `alertas_enviados` com `created_at < antes_de` e devolve
    quantas saíram (ou sairiam, em `dry_run`). Commita a cada lote de até
    `lote` linhas — ver `auth/protecao.limpar_tentativas_antigas` para o
    motivo do lote/commit por lote e do default de `lote`. Ver
    `retencao/cli.py` (issue #39).

    `mensagem` guarda o texto enviado, incluindo o nome do paciente (ver
    `db/models/alerta.py`) — dado pessoal de saúde retido para sempre não é
    neutro, é exposição que só cresce.
    """
    condicao = AlertaEnviado.created_at < antes_de
    if dry_run:
        total = session.scalar(select(func.count()).select_from(AlertaEnviado).where(condicao))
        return int(total or 0)

    total = 0
    while True:
        subquery = select(AlertaEnviado.id).where(condicao).limit(lote)
        resultado = cast(
            "CursorResult[Any]",
            session.execute(delete(AlertaEnviado).where(AlertaEnviado.id.in_(subquery))),
        )
        session.commit()
        apagadas = resultado.rowcount
        total += apagadas
        if apagadas < lote:
            break
    return total
