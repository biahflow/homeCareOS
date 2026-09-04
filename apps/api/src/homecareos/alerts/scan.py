"""Varredura de alertas para o cron: `python -m homecareos.alerts.scan`.

Escreve o `ResumoVarredura` como JSON em **stdout** e o erro de configuração em
**stderr**, saindo com código `1` — é o par que um cron consegue monitorar sem
precisar interpretar texto. Código `0` com `"enviados": 0` é resultado normal
(nada a avisar); código `1` significa "a configuração está quebrada e ninguém
está sendo avisado", que é a única situação em que alguém precisa acordar.

Falha de gateway **não** sai com `1`: ela já está registrada como linha `falha`
em `alertas_enviados`, e uma varredura em que um destinatário falhou e outros
três foram avisados não é uma execução fracassada. Canal desligado ou sem
credencial também não: os dois estados saem no JSON, canal a canal, em
`canais` (ADR 0006) — e desde a parte 2 do ADR o "desligado" vem da tabela
`canais_alerta`, não mais de `ALERTAS_CANAIS`.
"""

from __future__ import annotations

import sys

from homecareos.alerts.canais import construir_canais
from homecareos.alerts.errors import AlertConfigError
from homecareos.alerts.service import executar_varredura
from homecareos.config import get_settings
from homecareos.db.session import get_sessionmaker


def main() -> int:
    settings = get_settings()
    try:
        with get_sessionmaker()() as session:
            # `construir_canais` lê o liga/desliga da tabela `canais_alerta`
            # (ADR 0006, parte 2) e por isso precisa da sessão — antes ele vinha
            # de `ALERTAS_CANAIS` e era resolvido antes de abrir conexão. O
            # `try` continua envolvendo tudo porque `executar_varredura` valida
            # a configuração de alertas que sobrou no `.env` (destinatários,
            # papéis, templates): um typo lá precisa virar mensagem em stderr e
            # código 1, não traceback — é o que o cron monitora.
            canais = construir_canais(session, settings)
            resumo = executar_varredura(session, settings, canais)
    except AlertConfigError as exc:
        print(f"configuração de alertas inválida: {exc}", file=sys.stderr)
        return 1

    print(resumo.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
