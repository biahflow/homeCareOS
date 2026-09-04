"""Vocabulário do resumo do expurgo — mesma forma de `alerts/schema.ResumoVarredura`."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ResultadoTabela(BaseModel):
    """O que aconteceu (ou aconteceria, em `dry_run`) numa tabela."""

    apagadas: int
    corte: datetime


class ResumoExpurgo(BaseModel):
    """O que uma execução do expurgo fez, na forma que o cron e o CLI devolvem."""

    dry_run: bool
    executado_em: datetime
    tabelas: dict[str, ResultadoTabela]
