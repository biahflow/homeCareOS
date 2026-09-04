"""Os recursos limitados, os limites configurados e o resultado do freio.

`Recurso` é o enum que fecha a escrita de `consumos_rate_limit.recurso`, que é
`String` no banco — ver a docstring de `db/models/consumo_rate_limit.py` para a
razão de não ser tipo enum nativo do Postgres.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from homecareos.config import Settings


class Recurso(enum.StrEnum):
    """As quatro rotas caras do ADR 0005, na ordem de prioridade dele.

    O valor é o que vai para `consumos_rate_limit.recurso`; o `rotulo` é o que
    aparece na mensagem do 429 — ao contrário do 429 do login, que é
    deliberadamente genérico para não virar oráculo de "esta conta existe",
    aqui quem chegou já está autenticado como si mesmo e esconder qual limite
    estourou só atrapalha quem precisa se corrigir.
    """

    #: `POST /api/documentos` — dispara extração por IA síncrona: cada upload é
    #: uma chamada paga a um provider externo. É a única rota do sistema em que
    #: o abuso tem custo em dinheiro.
    UPLOAD_DOCUMENTO = "upload_documento"
    #: `GET /api/relatorios/conferencia.csv` — o extrato inteiro do filtro, sem
    #: paginação.
    RELATORIO_CSV = "relatorio_csv"
    #: `GET /api/documentos/{id}/arquivo` — streaming que ocupa um worker
    #: enquanto transmite (ADR 0003).
    DOWNLOAD_ARQUIVO = "download_arquivo"
    #: `POST /api/alertas/varredura` — roda os detectores e fala com o gateway
    #: de WhatsApp, enviando mensagem de verdade.
    VARREDURA_ALERTAS = "varredura_alertas"

    @property
    def rotulo(self) -> str:
        """Nome legível do recurso, para a mensagem do 429."""
        return _ROTULOS[self]


_ROTULOS: dict[Recurso, str] = {
    Recurso.UPLOAD_DOCUMENTO: "upload de documento",
    Recurso.RELATORIO_CSV: "exportação do relatório de conferência em CSV",
    Recurso.DOWNLOAD_ARQUIVO: "download do arquivo do documento",
    Recurso.VARREDURA_ALERTAS: "varredura de alertas",
}


@dataclass(frozen=True)
class LimitesDoRecurso:
    """Os dois limites por hora de um recurso: o de pessoa e o de máquina.

    São dois, e não um, porque a chave de máquina (`X-API-Key`) tem padrão de
    uso legítimo e repetitivo — é a credencial das integrações. O ADR determina
    que ela receba limites próprios e mais folgados.
    """

    pessoa: int
    maquina: int


def limites_do_recurso(settings: Settings, recurso: Recurso) -> LimitesDoRecurso:
    """Os limites configurados para `recurso`.

    Tabela explícita e não `getattr(settings, f"limite_{recurso}...")`: o nome
    montado em string passa por qualquer verificação de tipo e só falha em
    produção, no primeiro acesso à rota. Aqui, um `Recurso` novo sem limite
    configurado estoura `KeyError` — e o teste
    `test_todo_recurso_tem_limite_configurado` estoura antes, na suíte.
    """
    tabela: dict[Recurso, LimitesDoRecurso] = {
        Recurso.UPLOAD_DOCUMENTO: LimitesDoRecurso(
            pessoa=settings.limite_upload_documento_pessoa_por_hora,
            maquina=settings.limite_upload_documento_maquina_por_hora,
        ),
        Recurso.RELATORIO_CSV: LimitesDoRecurso(
            pessoa=settings.limite_relatorio_csv_pessoa_por_hora,
            maquina=settings.limite_relatorio_csv_maquina_por_hora,
        ),
        Recurso.DOWNLOAD_ARQUIVO: LimitesDoRecurso(
            pessoa=settings.limite_download_arquivo_pessoa_por_hora,
            maquina=settings.limite_download_arquivo_maquina_por_hora,
        ),
        Recurso.VARREDURA_ALERTAS: LimitesDoRecurso(
            pessoa=settings.limite_varredura_alertas_pessoa_por_hora,
            maquina=settings.limite_varredura_alertas_maquina_por_hora,
        ),
    }
    return tabela[recurso]


@dataclass(frozen=True)
class LimiteEstourado:
    """Resultado de `protecao.avaliar_limite` quando a requisição não pode passar.

    `segundos_restantes` é **calculado**, não fixo: é a janela menos a idade do
    consumo mais antigo dentro dela — o instante em que a cota de fato volta.
    Um `Retry-After` que manda esperar mais do que o necessário treina a pessoa
    a ignorá-lo.
    """

    recurso: Recurso
    limite: int
    segundos_restantes: int
