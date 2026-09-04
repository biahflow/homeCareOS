"""Orquestração do expurgo por retenção (issue #39): a trava mínima, os lotes
e o resumo.

O expurgo em si — a função que apaga (ou, em `dry_run`, só conta) cada tabela
— vive junto do domínio dela (`auth/protecao.py`, `auth/recuperacao.py`,
`alerts/repository.py`), no mesmo padrão de
`auth.protecao.limpar_tentativas_antigas`. Este módulo só decide QUANDO é
seguro chamar essas funções: valida a retenção configurada contra a janela de
segurança mínima de cada tabela (`retencao/janelas.py`) antes de apagar
qualquer coisa, resolve `--tabela`/lote/dry-run e monta o resumo.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy.orm import Session

from homecareos.alerts.repository import limpar_alertas_antigos
from homecareos.auth.protecao import limpar_tentativas_antigas
from homecareos.auth.recuperacao import limpar_tokens_antigos
from homecareos.config import Settings
from homecareos.retencao.errors import RetencaoConfigError, RetencaoInvalidaError
from homecareos.retencao.janelas import (
    FATOR_MARGEM_SEGURANCA,
    JanelaSeguranca,
    janelas_alertas_enviados,
    janelas_tentativas_login,
    janelas_tokens_recuperacao,
)
from homecareos.retencao.schema import ResultadoTabela, ResumoExpurgo


class _FuncaoApagar(Protocol):
    """Assinatura comum às três funções de apagar por idade, uma por domínio."""

    def __call__(
        self, session: Session, *, antes_de: datetime, agora: datetime, lote: int, dry_run: bool
    ) -> int: ...


@dataclass(frozen=True)
class Tabela:
    """Uma tabela expurgável: como saber a retenção, as janelas mínimas e como apagar."""

    chave: str
    retencao_dias: Callable[[Settings], int]
    janelas: Callable[[Settings], list[JanelaSeguranca]]
    apagar: _FuncaoApagar


def _apagar_tentativas_login(
    session: Session, *, antes_de: datetime, agora: datetime, lote: int, dry_run: bool
) -> int:
    del agora  # tentativas_login não tem exceção por idade — só o corte.
    return limpar_tentativas_antigas(session, antes_de=antes_de, lote=lote, dry_run=dry_run)


def _apagar_tokens_recuperacao(
    session: Session, *, antes_de: datetime, agora: datetime, lote: int, dry_run: bool
) -> int:
    return limpar_tokens_antigos(
        session, antes_de=antes_de, agora=agora, lote=lote, dry_run=dry_run
    )


def _apagar_alertas_enviados(
    session: Session, *, antes_de: datetime, agora: datetime, lote: int, dry_run: bool
) -> int:
    del agora  # alertas_enviados não tem exceção por idade — só o corte.
    return limpar_alertas_antigos(session, antes_de=antes_de, lote=lote, dry_run=dry_run)


TABELAS: tuple[Tabela, ...] = (
    Tabela(
        chave="tentativas_login",
        retencao_dias=lambda s: s.retencao_tentativas_login_dias,
        janelas=janelas_tentativas_login,
        apagar=_apagar_tentativas_login,
    ),
    Tabela(
        chave="tokens_recuperacao",
        retencao_dias=lambda s: s.retencao_tokens_recuperacao_dias,
        janelas=janelas_tokens_recuperacao,
        apagar=_apagar_tokens_recuperacao,
    ),
    Tabela(
        chave="alertas_enviados",
        retencao_dias=lambda s: s.retencao_alertas_enviados_dias,
        janelas=janelas_alertas_enviados,
        apagar=_apagar_alertas_enviados,
    ),
)

NOMES_TABELAS: frozenset[str] = frozenset(t.chave for t in TABELAS)


def _resolver_tabelas(tabelas: Sequence[str] | None) -> list[Tabela]:
    if tabelas is None:
        return list(TABELAS)
    por_chave = {t.chave: t for t in TABELAS}
    desconhecidas = sorted(set(tabelas) - por_chave.keys())
    if desconhecidas:
        raise RetencaoConfigError(
            f"tabela(s) desconhecida(s): {', '.join(desconhecidas)}. "
            f"Válidas: {', '.join(sorted(NOMES_TABELAS))}."
        )
    return [por_chave[chave] for chave in tabelas]


def _verificar_piso(tabela: Tabela, retencao: timedelta, settings: Settings) -> None:
    for janela in tabela.janelas(settings):
        if retencao < janela.piso_minimo:
            raise RetencaoInvalidaError(
                f"retenção configurada para '{tabela.chave}' ({retencao}) é menor que o "
                f"mínimo aceitável ({janela.piso_minimo}) para não desarmar "
                f"'{janela.descricao}' (janela ativa: {janela.janela}, origem: "
                f"{janela.origem}, margem de segurança aplicada: {FATOR_MARGEM_SEGURANCA}x)."
            )


def expurgar(
    session: Session,
    settings: Settings,
    *,
    tabelas: Sequence[str] | None = None,
    lote: int,
    dry_run: bool,
    agora: datetime,
) -> ResumoExpurgo:
    """Expurga (ou, em `dry_run`, só conta) as tabelas pedidas — todas, por padrão.

    Valida o piso mínimo de TODAS as tabelas selecionadas antes de apagar
    qualquer uma: uma retenção inválida numa tabela não pode deixar outra já
    parcialmente apagada. A validação roda mesmo em `dry_run` — é o que torna
    o dry-run capaz de flagrar a configuração errada sem tocar o banco.
    """
    if lote <= 0:
        raise RetencaoConfigError(f"lote precisa ser positivo, recebi {lote}.")

    alvo = _resolver_tabelas(tabelas)
    cortes: dict[str, tuple[Tabela, datetime]] = {}
    for tabela in alvo:
        retencao = timedelta(days=tabela.retencao_dias(settings))
        _verificar_piso(tabela, retencao, settings)
        cortes[tabela.chave] = (tabela, agora - retencao)

    resultados: dict[str, ResultadoTabela] = {}
    for chave, (tabela, antes_de) in cortes.items():
        apagadas = tabela.apagar(
            session, antes_de=antes_de, agora=agora, lote=lote, dry_run=dry_run
        )
        resultados[chave] = ResultadoTabela(apagadas=apagadas, corte=antes_de)

    return ResumoExpurgo(dry_run=dry_run, executado_em=agora, tabelas=resultados)
