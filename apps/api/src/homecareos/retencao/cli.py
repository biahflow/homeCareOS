"""Expurgo por retenção — issue #39: `python -m homecareos.retencao.cli`.

Ferramenta sob demanda para as três tabelas que crescem sem limite
(`tentativas_login`, `tokens_recuperacao`, `alertas_enviados`) e não tinham
expurgo automático. Chamada por um cron EXTERNO, como
`python -m homecareos.alerts.scan` — não há agendador embutido nesta entrega
(ver `docker-compose.yml`, serviço `api-retencao`, e a seção "Retenção e
expurgo de dados" no README de apps/api).

## `--dry-run` é o padrão

Sem `--executar`, o comando só CONTA e reporta — nada é apagado. É de
propósito: isto é um `DELETE` sobre dado de segurança e auditoria (tentativa
de login, token de recuperação, mensagem com nome de paciente), e a primeira
execução contra um banco de produção precisa ser uma decisão informada, não um
salto no escuro. O contra-argumento é real — um cron que rodasse em dry-run
por engano não expurgaria nada e ninguém perceberia por meses —, mas o resumo
sempre diz `"dry_run": true/false` explicitamente, então essa configuração
errada é auditável em segundos a partir do próprio log do cron, ao custo de
uma checagem que hoje não existe.

## Saída

Resumo em JSON em **stdout** (padrão de `alerts/scan.py`), erro em
**stderr**, código `1` quando a retenção configurada viola o piso mínimo de
alguma janela de segurança (ver `retencao/janelas.py`) ou quando um argumento
é inválido (tabela desconhecida, lote não positivo).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from homecareos.config import get_settings
from homecareos.db.session import get_sessionmaker
from homecareos.retencao.errors import RetencaoError
from homecareos.retencao.service import NOMES_TABELAS, expurgar


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m homecareos.retencao.cli",
        description=(
            "Expurga linhas antigas de tentativas_login, tokens_recuperacao e "
            "alertas_enviados, respeitando a janela mínima de segurança de cada "
            "uma (issue #39)."
        ),
    )
    parser.add_argument(
        "--tabela",
        choices=sorted(NOMES_TABELAS),
        action="append",
        help="Restringe o expurgo a uma tabela (repetível). Default: as três.",
    )
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument(
        "--dry-run",
        action="store_true",
        help="Só conta e reporta, sem apagar (comportamento default, mesmo sem esta flag).",
    )
    modo.add_argument(
        "--executar",
        action="store_true",
        help="Apaga de verdade. Sem esta flag, o comando nunca muda o banco.",
    )
    parser.add_argument(
        "--lote",
        type=int,
        default=None,
        help="Tamanho do lote de apagar (default: configuração RETENCAO_TAMANHO_LOTE).",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    lote = args.lote if args.lote is not None else settings.retencao_tamanho_lote

    try:
        with get_sessionmaker()() as session:
            resumo = expurgar(
                session,
                settings,
                tabelas=args.tabela,
                lote=lote,
                dry_run=not args.executar,
                agora=datetime.now(UTC),
            )
    except RetencaoError as exc:
        print(f"expurgo de retenção inválido: {exc}", file=sys.stderr)
        return 1

    print(resumo.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
