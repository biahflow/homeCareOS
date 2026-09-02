"""Enums nativos do Postgres compartilhados pelos models de conferência."""

import enum


class Modalidade(enum.StrEnum):
    """Modalidade de atendimento home care do paciente."""

    AD = "AD"
    ID = "ID"


class TipoDocumento(enum.StrEnum):
    """Tipo do documento ingerido para conferência pré-faturamento."""

    EVOLUCAO = "evolucao"
    FICHA_VISITA = "ficha_visita"
    BOLETIM = "boletim"
    MATMED = "matmed"


class DocumentoStatus(enum.StrEnum):
    """Status do documento no ciclo de conferência pré-faturamento.

    Ciclos válidos (a máquina de estados que impõe as transições fica fora
    de escopo desta trilha — issue #2 cobre só a modelagem de dados):

        processando -> aprovado
            (documento aprovado direto, segue pro faturamento)

        processando -> problema    -> em_correcao -> resolvido -> liberado
        processando -> incompleto  -> em_correcao -> resolvido -> liberado
    """

    PROCESSANDO = "processando"
    APROVADO = "aprovado"
    PROBLEMA = "problema"
    INCOMPLETO = "incompleto"
    EM_CORRECAO = "em_correcao"
    RESOLVIDO = "resolvido"
    LIBERADO = "liberado"


class ResultadoValidacao(enum.StrEnum):
    """Resultado da aplicação de uma regra de operadora sobre um documento."""

    APROVADO = "aprovado"
    REPROVADO = "reprovado"


class PendenciaStatus(enum.StrEnum):
    """Status de uma pendência aberta sobre um documento."""

    ABERTA = "aberta"
    EM_CORRECAO = "em_correcao"
    RESOLVIDA = "resolvida"
