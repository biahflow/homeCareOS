"""Vocabulário de campo -> rótulo humano, para o texto dos alertas de WhatsApp (issue #9).

Onze campos de `Pendencia.campo` podem virar pendência (o mesmo conjunto que
`rules/data/*.json` usa e que `rules/catalogo.py` valida contra
`EvolucaoProntuario.model_fields`, em `extraction/schema.py`). Este mapa escreve
cada um como a equipe de conferência fala — "Assinatura do profissional", não
`assinatura_profissional_presente` — no mesmo espírito de
`apps/web/components/documentos/vocabulario.ts`, que não tem equivalente no
backend: rótulo é rótulo, não recria regra de produto.

## Por que este mapa mora em `alerts/`, e não num lugar mais "de domínio"

O vocabulário em si é de domínio (nasce do nome dos campos de
`EvolucaoProntuario`), mas `alerts/` é o **único** consumidor real hoje. O
`problema_encontrado` do relatório de conferência (`reports/conferencia.py`)
continua usando `Pendencia.descricao` de propósito — é outro texto, escrito
para outra tela (ver `classification/engine._descricao`), e esta entrega não
mexe nele. Promover este mapa para fora de `alerts/` quando um segundo
consumidor real precisar dele é o próximo passo; criar essa camada agora, sem
esse segundo consumidor, seria abstração especulativa.

## Campo fora do mapa não é erro

`rotulo_de_campo` devolve o próprio nome técnico do campo quando ele não está
aqui: uma regra nova, criada pela API depois desta entrega, ainda não tem
rótulo bonito, mas **continua** produzindo alerta — feio, mas visível. Um
alerta que engole um problema porque não conhecia o nome do campo é pior que
um alerta feio.
"""

from __future__ import annotations

ROTULOS_DE_CAMPO: dict[str, str] = {
    "assinatura_paciente_responsavel_presente": "Assinatura do responsável",
    "assinatura_profissional_presente": "Assinatura do profissional",
    "carimbo_legivel": "Carimbo legível",
    "carimbo_presente": "Carimbo",
    "categoria_profissional": "Categoria profissional",
    "data_atendimento": "Data do atendimento",
    "materiais_utilizados": "Materiais utilizados",
    "nome_paciente": "Nome do paciente",
    "nome_profissional": "Nome do profissional",
    "procedimentos_realizados": "Procedimentos realizados",
    "registro_coren": "Registro no COREN",
}

CAMPO_NAO_IDENTIFICADO = "campo não identificado"
"""Fallback para `Pendencia.campo is None` — pendência anterior à issue #7, que
nunca teve campo conhecido (`Pendencia.campo` é nullable, ver
`db/models/pendencia.py`). Diferente de um campo fora do vocabulário (que tem
nome, só não tem rótulo bonito ainda): aqui não há nem o nome."""


def rotulo_de_campo(campo: str | None) -> str:
    """Rótulo legível de um campo de pendência, para o texto de um alerta.

    Campo fora do vocabulário conhecido devolve o próprio nome técnico — feio,
    mas visível, e é o que garante que o alerta não desapareça só porque uma
    regra nova ainda não ganhou rótulo. `None`/vazio devolve
    `CAMPO_NAO_IDENTIFICADO`.
    """
    if not campo:
        return CAMPO_NAO_IDENTIFICADO
    return ROTULOS_DE_CAMPO.get(campo, campo)
