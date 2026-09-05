"""fonte correta e regex sem UF fixa nas regras candidatas de operadora (#39)

Revision ID: 81397b5c0ce8
Revises: a4d6c8b21f37
Create Date: 2026-09-05 00:00:00.000000

As 12 regras candidatas de operadora (6 Amil + 6 Unimed, `escopo='operadora'`,
`ativo=false`) tinham `fonte` começando por "A CONFIRMAR — candidata derivada
do padrão TISS/ANS...". Dois erros nesse texto e um defeito de regex, achados
numa auditoria de procedência (#39):

1. **Atribuição falsa ao TISS.** O padrão TISS/ANS padroniza a troca eletrônica
   de guias (XML) entre operadora e prestador — não é fonte de exigência
   nenhuma sobre o documento físico de evolução (carimbo, registro Coren,
   assinatura). Para parte dessas regras a fonte real é a Resolução Cofen
   545/2017 (art. 2º, §1º, §2º e art. 5º, III); para a outra parte, não há
   norma pública confirmada e o texto agora diz isso sem meias-palavras.
2. **Limite da norma não declarado.** Mesmo onde a 545/2017 é pública e
   confirmada, ela obriga o **profissional de enfermagem** — não define o que
   a **operadora glosa**. Por isso as 12 continuam `ativo=false`: esta
   migration corrige procedência, não ativa regra nenhuma. Ver
   `rules/data/amil.json` e `rules/data/unimed.json` para o texto completo, e
   a seção "Catálogo de regras" de `apps/api/README.md`.

**Defeito:** `AMIL-AD-COREN-FORMATO` tinha a regex travada em `COREN-RJ`,
recusando como pendência falsa um Coren de outra UF (ex.: `COREN-SP`) mesmo
quando o atendimento é no Rio — a Resolução Cofen 545/2017, art. 2º, manda
anotar a UF **do Conselho Regional onde o profissional se inscreveu**, não a
UF do atendimento. Corrigido para `[A-Za-z]{2}`, igual à genérica
`TISS-EVOL-COREN-FORMATO` e à candidata da Unimed (que já não tinha o defeito).
`motivo_glosa` não mudou: já dizia só "UF e categoria explícitas", sem fixar
`RJ`.

## Conflito de cabeça conhecido, com a cifra do segredo TOTP

Esta migration e `f2b9d6e04a17` (cifra do segredo TOTP, ADR 0008) nasceram do
mesmo pai, `a4d6c8b21f37`, em entregas paralelas. **Juntas em `main` elas
produzem duas cabeças**, e `alembic upgrade head` falha com "Multiple head
revisions are present" — nenhuma migration roda.

Encadear uma na outra aqui foi testado e **descartado**: a branch de baixo não
contém a revision de cima, então o alembic nem monta o grafo (`KeyError`) e a
PR fica com o CI vermelho até a outra ser mesclada.

A resolução é no merge: **quem for mesclado por segundo troca o próprio
`down_revision` pela revision que entrou primeiro.** É uma linha, e o teste de
cabeça única em `tests/test_migrations.py` reprova se alguém esquecer.

## Por que migration, e não só o JSON

`rules/seed_regras.seed_regras` é `INSERT ... ON CONFLICT DO NOTHING` e
**nunca** `DO UPDATE` — a própria docstring do módulo diz que mudar conteúdo de
regra já cadastrada é migration de dados explícita, não efeito colateral de
seed. Editar só `rules/data/*.json` não alcança banco nenhum que já tenha
rodado o seed antes desta entrega.

## Como as linhas são casadas

Por `codigo` + `escopo = 'operadora'`, não por `operadora_id`: o seed
materializa uma linha por operadora (`seed_regras._linha`), então em tese mais
de uma linha pode compartilhar o mesmo `codigo` (ex.: duas operadoras com o
mesmo `operadoras.codigo`, caso hipotético hoje não presente na base local).
O `UPDATE` por `codigo` alcança todas elas, sem precisar descobrir
`operadora_id`.

A atualização de `condicao` (só `AMIL-AD-COREN-FORMATO`) tem uma guarda a mais:
só troca a linha cujo `condicao` ainda é exatamente o valor antigo serializado
pelo schema (`CondicaoTypeAdapter.model_dump_json()`, o mesmo caminho de
`seed_regras._condicao_json`). `RegraUpdate` (`rules/schema.py`) inclui
`condicao` — ao contrário de `fonte`, `codigo` e `escopo`, que a API nunca
expõe para escrita —, então uma operação já pode ter ajustado esta regex à mão
via `PUT /api/regras/{id}` (é o cenário que a docstring de `seed_regras`
cita: "afrouxa um regex"). Sem a guarda, esta migration sobrescreveria esse
ajuste manual em silêncio.

`fonte` não tem essa guarda: a API não expõe escrita nela (`RegraCreate`/
`RegraUpdate` não têm o campo), então o valor em banco só pode ser o do
catálogo — e o `UPDATE` por `codigo` + `escopo` é seguro sem checar o texto
atual.

**`ativo` não é tocado em lugar nenhum desta migration.** É a coluna que a
operação ajusta à mão (desativar uma regra que gera ruído), e tanto o seed
quanto esta migration são desenhados para nunca desfazer essa decisão.

## Reversibilidade

`downgrade()` restaura, para as mesmas 12 linhas, o texto de `fonte` anterior
a esta migration (o "A CONFIRMAR — candidata derivada do padrão TISS/ANS...",
preservado aqui em `FONTES` só para o downgrade) e, para
`AMIL-AD-COREN-FORMATO`, a `condicao` com a regex travada em `RJ` — a mesma
guarda por valor atual se aplica, no sentido inverso.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "81397b5c0ce8"
down_revision: str | None = "a4d6c8b21f37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Serialização canônica de `condicao` (mesmo caminho que
# `rules/seed_regras._condicao_json` usa: `CondicaoTypeAdapter.validate_python`
# + `model_dump_json()`), só para `AMIL-AD-COREN-FORMATO` — a única `condicao`
# que este defeito atinge. Quebrado em literais adjacentes (concatenação
# implícita do Python) só para caber no limite de linha do ruff — o valor
# depois da concatenação é exatamente o mesmo, uma única string.
CONDICAO_ANTIGA = (
    '{"campo":null,"tipo":"formato","regex":"COREN[-/ '
    ']?RJ[\\\\s.:\\\\-]*\\\\d{4,7}[\\\\s.\\\\-]*(ENF|TE|AE)?"}'
)
CONDICAO_NOVA = (
    '{"campo":null,"tipo":"formato","regex":"COREN[-/ '
    ']?[A-Za-z]{2}[\\\\s.:\\\\-]*\\\\d{4,7}[\\\\s.\\\\-]*(ENF|TE|AE)?"}'
)

# (fonte_antiga, fonte_nova) por código — texto completo em
# `rules/data/amil.json` e `rules/data/unimed.json`. Cada string longa vem
# quebrada em literais adjacentes pelo mesmo motivo do bloco acima.
FONTES: dict[str, tuple[str, str]] = {
    "AMIL-AD-COREN-FORMATO": (
        "A CONFIRMAR — candidata derivada do padrão TISS/ANS e da prática do setor; NÃO verificada "
        "no manual do prestador da Amil para atenção domiciliar, que não é público. Nasce "
        "ativo=false: ativar somente depois de conferir a exigência no manual vigente e registrar "
        "aqui a seção e a data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-COREN-FORMATO desta mesma operadora, que ela endurece.",
        "A CONFIRMAR — formato do número do Coren CONFIRMADO em norma pública: Resolução Cofen "
        "545/2017, art. 2º, manda anotar com a sigla COREN, a sigla da UF onde está sediado o "
        "Conselho Regional em que o profissional se inscreveu (não a UF do atendimento) e o número "
        "de inscrição, separados por hífen. https://www.cofen.gov.br/resolucao-cofen-no-05452017/ "
        "(consultado em 05/09/2026). A norma obriga o profissional de enfermagem a se identificar "
        "assim; ela não define o que a Amil glosa, e ativar esta regra depende do manual do "
        "prestador da Amil para atenção domiciliar, que não é público. O que continua A CONFIRMAR "
        "é a ação: glosar por desvio de formato é regra desse manual. Enquanto isso, a regra "
        "genérica TISS-EVOL-COREN-FORMATO cobre o campo com sinalizar. Nasce ativo=false: ativar "
        "somente depois de conferir a exigência de glosa no manual vigente e registrar aqui a "
        "seção e a data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-COREN-FORMATO desta mesma operadora, que ela endurece.",
    ),
    "AMIL-AD-CARIMBO-LEGIVEL": (
        "A CONFIRMAR — candidata derivada do padrão TISS/ANS e da prática do setor; NÃO verificada "
        "no manual do prestador da Amil para atenção domiciliar, que não é público. Nasce "
        "ativo=false: ativar somente depois de conferir a exigência no manual vigente e registrar "
        "aqui a seção e a data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-CARIMBO-LEGIVEL desta mesma operadora, que ela endurece.",
        "A CONFIRMAR — obrigatoriedade do carimbo CONFIRMADA em norma pública: Resolução Cofen "
        "545/2017, art. 2º §1º e §2º (os dados constam do carimbo pessoal e intransferível, com "
        "assinatura ou rubrica sobre eles) e art. 5º, III (obrigatório em todo documento firmado "
        "no exercício profissional, em cumprimento ao Código de Ética — hoje Resolução Cofen "
        "564/2017). https://www.cofen.gov.br/resolucao-cofen-no-05452017/ (consultado em "
        "05/09/2026). A norma obriga o profissional a carimbar; ela não define o que a Amil glosa, "
        "e ativar esta regra depende do manual do prestador da Amil para atenção domiciliar, que "
        "não é público. A legibilidade do carimbo é inferência razoável deste time (carimbo "
        "ilegível não cumpre a função de identificar), não texto da norma. O que continua A "
        "CONFIRMAR é a ação: glosar por carimbo ilegível é regra desse manual. Enquanto isso, a "
        "regra genérica TISS-EVOL-CARIMBO-LEGIVEL cobre o campo com sinalizar. Nasce ativo=false: "
        "ativar somente depois de conferir a exigência de glosa no manual vigente e registrar aqui "
        "a seção e a data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-CARIMBO-LEGIVEL desta mesma operadora, que ela endurece.",
    ),
    "AMIL-AD-ASSINATURA-RESPONSAVEL": (
        "A CONFIRMAR — candidata derivada do padrão TISS/ANS e da prática do setor; NÃO verificada "
        "no manual do prestador da Amil para atenção domiciliar, que não é público. Nasce "
        "ativo=false: ativar somente depois de conferir a exigência no manual vigente e registrar "
        "aqui a seção e a data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-ASSINATURA-RESPONSAVEL desta mesma operadora, que ela endurece.",
        "A CONFIRMAR — exigência NÃO confirmada em norma pública (consultado em 05/09/2026): a "
        "Resolução Cofen 545/2017 trata da identificação do profissional (carimbo e registro), não "
        "da assinatura do paciente ou responsável — não há norma de conselho que decida isto. A "
        "exigência existe na prática do setor como comprovação de execução (ficha de "
        "produtividade, guia de faturamento assinada), mas a norma do Cofen obriga o profissional, "
        "não define o que a Amil glosa, e ativar esta regra depende do manual do prestador da Amil "
        "para atenção domiciliar, que não é público. Enquanto isso, a regra genérica "
        "TISS-EVOL-ASSINATURA-RESPONSAVEL cobre o campo com sinalizar. Nasce ativo=false: ativar "
        "somente depois de conferir a exigência no manual vigente e registrar aqui a seção e a "
        "data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-ASSINATURA-RESPONSAVEL desta mesma operadora, que ela endurece.",
    ),
    "AMIL-AD-DATA-COMPETENCIA": (
        "A CONFIRMAR — candidata derivada do padrão TISS/ANS e da prática do setor; NÃO verificada "
        "no manual do prestador da Amil para atenção domiciliar, que não é público. Nasce "
        "ativo=false: ativar somente depois de conferir a exigência no manual vigente e registrar "
        "aqui a seção e a data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-DATA-COMPETENCIA desta mesma operadora, que ela endurece.",
        "A CONFIRMAR — exigência NÃO confirmada em norma pública (consultado em 05/09/2026): "
        "registro de enfermagem datado é prática consolidada, mas o vínculo entre a data do "
        "atendimento e a competência do lote é regra de faturamento da operadora, não norma de "
        "conselho — a norma do Cofen obriga o profissional a registrar a data, não define o que a "
        "Amil glosa por competência. Ativar esta regra depende do manual do prestador da Amil para "
        "atenção domiciliar, que não é público. Enquanto isso, a regra genérica "
        "TISS-EVOL-DATA-COMPETENCIA cobre o campo com sinalizar. Nasce ativo=false: ativar somente "
        "depois de conferir a exigência no manual vigente e registrar aqui a seção e a data da "
        "consulta. Ao ativar, avalie desativar a regra genérica TISS-EVOL-DATA-COMPETENCIA desta "
        "mesma operadora, que ela endurece.",
    ),
    "AMIL-AD-MATERIAIS": (
        "A CONFIRMAR — candidata derivada do padrão TISS/ANS e da prática do setor; NÃO verificada "
        "no manual do prestador da Amil para atenção domiciliar, que não é público. Nasce "
        "ativo=false: ativar somente depois de conferir a exigência no manual vigente e registrar "
        "aqui a seção e a data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-MATERIAIS desta mesma operadora, que ela endurece.",
        "A CONFIRMAR — exigência NÃO confirmada em norma pública (consultado em 05/09/2026): "
        "nenhuma norma pública de conselho profissional exige lista de materiais na evolução; é "
        "exigência de faturamento, específica de contrato — a norma do Cofen obriga o profissional "
        "quanto ao registro clínico, não define o que a Amil glosa por material. Ativar esta regra "
        "depende do manual do prestador da Amil para atenção domiciliar, que não é público. "
        "Enquanto isso, a regra genérica TISS-EVOL-MATERIAIS cobre o campo com sinalizar. Nasce "
        "ativo=false: ativar somente depois de conferir a exigência no manual vigente e registrar "
        "aqui a seção e a data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-MATERIAIS desta mesma operadora, que ela endurece.",
    ),
    "AMIL-AD-PROFISSIONAL-NOME-COMPLETO": (
        "A CONFIRMAR — candidata derivada do padrão TISS/ANS e da prática do setor; NÃO verificada "
        "no manual do prestador da Amil para atenção domiciliar, que não é público. Nasce "
        "ativo=false: ativar somente depois de conferir a exigência no manual vigente e registrar "
        "aqui a seção e a data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-PROFISSIONAL-NOME desta mesma operadora, que ela endurece.",
        "A CONFIRMAR — exigência NÃO confirmada em norma pública (consultado em 05/09/2026): a "
        "norma do Cofen obriga o profissional a se identificar, não define o que a Amil glosa por "
        "nome incompleto, e ativar esta regra depende do manual do prestador da Amil para atenção "
        "domiciliar, que não é público. Achado registrado: esta candidata é mais fraca que a "
        "genérica que deveria endurecer — TISS-EVOL-PROFISSIONAL-NOME já está ativa com "
        "acao=rejeitar, enquanto esta é sinalizar. Ativá-la como está afrouxaria a conferência em "
        "vez de endurecê-la; se um dia for ativada, a ação precisa ser revista antes. Nasce "
        "ativo=false: ativar somente depois de conferir a exigência no manual vigente e registrar "
        "aqui a seção e a data da consulta.",
    ),
    "UNIMED-AD-COREN-FORMATO": (
        "A CONFIRMAR — candidata derivada do padrão TISS/ANS e da prática do setor; NÃO verificada "
        "no manual do prestador da Unimed Rio para atenção domiciliar, que não é público. Nasce "
        "ativo=false: ativar somente depois de conferir a exigência no manual vigente e registrar "
        "aqui a seção e a data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-COREN-FORMATO desta mesma operadora, que ela endurece.",
        "A CONFIRMAR — formato do número do Coren CONFIRMADO em norma pública: Resolução Cofen "
        "545/2017, art. 2º, manda anotar com a sigla COREN, a sigla da UF onde está sediado o "
        "Conselho Regional em que o profissional se inscreveu (não a UF do atendimento) e o número "
        "de inscrição, separados por hífen. https://www.cofen.gov.br/resolucao-cofen-no-05452017/ "
        "(consultado em 05/09/2026). A norma obriga o profissional de enfermagem a se identificar "
        "assim; ela não define o que a Unimed Rio glosa, e ativar esta regra depende do manual do "
        "prestador da Unimed Rio para atenção domiciliar, que não é público. O que continua A "
        "CONFIRMAR é a ação: glosar por desvio de formato é regra desse manual. Enquanto isso, a "
        "regra genérica TISS-EVOL-COREN-FORMATO cobre o campo com sinalizar. Nasce ativo=false: "
        "ativar somente depois de conferir a exigência de glosa no manual vigente e registrar aqui "
        "a seção e a data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-COREN-FORMATO desta mesma operadora, que ela endurece.",
    ),
    "UNIMED-AD-CARIMBO-LEGIVEL": (
        "A CONFIRMAR — candidata derivada do padrão TISS/ANS e da prática do setor; NÃO verificada "
        "no manual do prestador da Unimed Rio para atenção domiciliar, que não é público. Nasce "
        "ativo=false: ativar somente depois de conferir a exigência no manual vigente e registrar "
        "aqui a seção e a data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-CARIMBO-LEGIVEL desta mesma operadora, que ela endurece.",
        "A CONFIRMAR — obrigatoriedade do carimbo CONFIRMADA em norma pública: Resolução Cofen "
        "545/2017, art. 2º §1º e §2º (os dados constam do carimbo pessoal e intransferível, com "
        "assinatura ou rubrica sobre eles) e art. 5º, III (obrigatório em todo documento firmado "
        "no exercício profissional, em cumprimento ao Código de Ética — hoje Resolução Cofen "
        "564/2017). https://www.cofen.gov.br/resolucao-cofen-no-05452017/ (consultado em "
        "05/09/2026). A norma obriga o profissional a carimbar; ela não define o que a Unimed Rio "
        "glosa, e ativar esta regra depende do manual do prestador da Unimed Rio para atenção "
        "domiciliar, que não é público. A legibilidade do carimbo é inferência razoável deste time "
        "(carimbo ilegível não cumpre a função de identificar), não texto da norma. O que continua "
        "A CONFIRMAR é a ação: glosar por carimbo ilegível é regra desse manual. Enquanto isso, a "
        "regra genérica TISS-EVOL-CARIMBO-LEGIVEL cobre o campo com sinalizar. Nasce ativo=false: "
        "ativar somente depois de conferir a exigência de glosa no manual vigente e registrar aqui "
        "a seção e a data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-CARIMBO-LEGIVEL desta mesma operadora, que ela endurece.",
    ),
    "UNIMED-AD-ASSINATURA-RESPONSAVEL": (
        "A CONFIRMAR — candidata derivada do padrão TISS/ANS e da prática do setor; NÃO verificada "
        "no manual do prestador da Unimed Rio para atenção domiciliar, que não é público. Nasce "
        "ativo=false: ativar somente depois de conferir a exigência no manual vigente e registrar "
        "aqui a seção e a data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-ASSINATURA-RESPONSAVEL desta mesma operadora, que ela endurece.",
        "A CONFIRMAR — exigência NÃO confirmada em norma pública (consultado em 05/09/2026): a "
        "Resolução Cofen 545/2017 trata da identificação do profissional (carimbo e registro), não "
        "da assinatura do paciente ou responsável — não há norma de conselho que decida isto. A "
        "exigência existe na prática do setor como comprovação de execução (ficha de "
        "produtividade, guia de faturamento assinada), mas a norma do Cofen obriga o profissional, "
        "não define o que a Unimed Rio glosa, e ativar esta regra depende do manual do prestador "
        "da Unimed Rio para atenção domiciliar, que não é público. Enquanto isso, a regra genérica "
        "TISS-EVOL-ASSINATURA-RESPONSAVEL cobre o campo com sinalizar. Nasce ativo=false: ativar "
        "somente depois de conferir a exigência no manual vigente e registrar aqui a seção e a "
        "data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-ASSINATURA-RESPONSAVEL desta mesma operadora, que ela endurece.",
    ),
    "UNIMED-AD-DATA-COMPETENCIA": (
        "A CONFIRMAR — candidata derivada do padrão TISS/ANS e da prática do setor; NÃO verificada "
        "no manual do prestador da Unimed Rio para atenção domiciliar, que não é público. Nasce "
        "ativo=false: ativar somente depois de conferir a exigência no manual vigente e registrar "
        "aqui a seção e a data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-DATA-COMPETENCIA desta mesma operadora, que ela endurece.",
        "A CONFIRMAR — exigência NÃO confirmada em norma pública (consultado em 05/09/2026): "
        "registro de enfermagem datado é prática consolidada, mas o vínculo entre a data do "
        "atendimento e a competência do lote é regra de faturamento da operadora, não norma de "
        "conselho — a norma do Cofen obriga o profissional a registrar a data, não define o que a "
        "Unimed Rio glosa por competência. Ativar esta regra depende do manual do prestador da "
        "Unimed Rio para atenção domiciliar, que não é público. Enquanto isso, a regra genérica "
        "TISS-EVOL-DATA-COMPETENCIA cobre o campo com sinalizar. Nasce ativo=false: ativar somente "
        "depois de conferir a exigência no manual vigente e registrar aqui a seção e a data da "
        "consulta. Ao ativar, avalie desativar a regra genérica TISS-EVOL-DATA-COMPETENCIA desta "
        "mesma operadora, que ela endurece.",
    ),
    "UNIMED-AD-MATERIAIS": (
        "A CONFIRMAR — candidata derivada do padrão TISS/ANS e da prática do setor; NÃO verificada "
        "no manual do prestador da Unimed Rio para atenção domiciliar, que não é público. Nasce "
        "ativo=false: ativar somente depois de conferir a exigência no manual vigente e registrar "
        "aqui a seção e a data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-MATERIAIS desta mesma operadora, que ela endurece.",
        "A CONFIRMAR — exigência NÃO confirmada em norma pública (consultado em 05/09/2026): "
        "nenhuma norma pública de conselho profissional exige lista de materiais na evolução; é "
        "exigência de faturamento, específica de contrato — a norma do Cofen obriga o profissional "
        "quanto ao registro clínico, não define o que a Unimed Rio glosa por material. Ativar esta "
        "regra depende do manual do prestador da Unimed Rio para atenção domiciliar, que não é "
        "público. Enquanto isso, a regra genérica TISS-EVOL-MATERIAIS cobre o campo com sinalizar. "
        "Nasce ativo=false: ativar somente depois de conferir a exigência no manual vigente e "
        "registrar aqui a seção e a data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-MATERIAIS desta mesma operadora, que ela endurece.",
    ),
    "UNIMED-AD-PROFISSIONAL-NOME-COMPLETO": (
        "A CONFIRMAR — candidata derivada do padrão TISS/ANS e da prática do setor; NÃO verificada "
        "no manual do prestador da Unimed Rio para atenção domiciliar, que não é público. Nasce "
        "ativo=false: ativar somente depois de conferir a exigência no manual vigente e registrar "
        "aqui a seção e a data da consulta. Ao ativar, avalie desativar a regra genérica "
        "TISS-EVOL-PROFISSIONAL-NOME desta mesma operadora, que ela endurece.",
        "A CONFIRMAR — exigência NÃO confirmada em norma pública (consultado em 05/09/2026): a "
        "norma do Cofen obriga o profissional a se identificar, não define o que a Unimed Rio "
        "glosa por nome incompleto, e ativar esta regra depende do manual do prestador da Unimed "
        "Rio para atenção domiciliar, que não é público. Achado registrado: esta candidata é mais "
        "fraca que a genérica que deveria endurecer — TISS-EVOL-PROFISSIONAL-NOME já está ativa "
        "com acao=rejeitar, enquanto esta é sinalizar. Ativá-la como está afrouxaria a conferência "
        "em vez de endurecê-la; se um dia for ativada, a ação precisa ser revista antes. Nasce "
        "ativo=false: ativar somente depois de conferir a exigência no manual vigente e registrar "
        "aqui a seção e a data da consulta.",
    ),
}


def upgrade() -> None:
    conn = op.get_bind()
    for codigo, (_fonte_antiga, fonte_nova) in FONTES.items():
        conn.execute(
            sa.text(
                "UPDATE regras SET fonte = :fonte WHERE codigo = :codigo AND escopo = 'operadora'"
            ),
            {"fonte": fonte_nova, "codigo": codigo},
        )

    # Só troca quem ainda está no valor antigo — ver docstring do módulo.
    conn.execute(
        sa.text(
            "UPDATE regras SET condicao = :nova "
            "WHERE codigo = 'AMIL-AD-COREN-FORMATO' AND escopo = 'operadora' "
            "AND condicao = :antiga"
        ),
        {"nova": CONDICAO_NOVA, "antiga": CONDICAO_ANTIGA},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE regras SET condicao = :antiga "
            "WHERE codigo = 'AMIL-AD-COREN-FORMATO' AND escopo = 'operadora' "
            "AND condicao = :nova"
        ),
        {"antiga": CONDICAO_ANTIGA, "nova": CONDICAO_NOVA},
    )

    for codigo, (fonte_antiga, _fonte_nova) in FONTES.items():
        conn.execute(
            sa.text(
                "UPDATE regras SET fonte = :fonte WHERE codigo = :codigo AND escopo = 'operadora'"
            ),
            {"fonte": fonte_antiga, "codigo": codigo},
        )
