"""Loader e validação do catálogo inicial de regras (issue #10).

Os JSON em `rules/data/` são a biblioteca de regras versionada no código —
procedência auditável, revisável em PR, sem depender de alguém digitar a regra
certa na API em produção. `seed_regras.py` é quem grava o catálogo no banco;
este módulo só carrega e valida.

Nota de pesquisa: o Painel de Indicadores de Glosa da ANS (dados abertos,
publicado em 27/11/2025) foi consultado e **não** serviu de fonte para regra
nenhuma: ele publica valores pagos/glosados e prazos por operadora, não os
motivos de glosa. Fica citado aqui para quem repetir a pesquisa não gastar o
tempo de novo.
https://www.gov.br/ans/pt-br/acesso-a-informacao/perfil-do-setor/dados-e-indicadores-do-setor/painel-de-indicadores-de-glosa
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from homecareos.extraction.schema import EvolucaoProntuario
from homecareos.rules.schema import AcaoRegra, CondicaoTypeAdapter, EscopoRegra

CATALOGO_TISS = "tiss_generico.json"
# A chave é `operadoras.codigo`.
CATALOGO_POR_OPERADORA: dict[str, str] = {"AMIL": "amil.json", "UNIMED": "unimed.json"}


class RegraCatalogo(BaseModel):
    """Uma linha do catálogo, já validada contra a gramática do motor e o schema de extração."""

    codigo: str = Field(min_length=1)
    campo: str = Field(min_length=1)
    condicao: dict[str, Any]
    acao: AcaoRegra
    motivo_glosa: str = Field(min_length=1)
    fonte: str = Field(min_length=1)
    escopo: EscopoRegra
    ativo: bool

    @field_validator("campo")
    @classmethod
    def _campo_existe_no_schema_de_extracao(cls, valor: str) -> str:
        # Mesmo portão que `rules/engine.py` aplica em runtime: campo que o
        # motor não sabe ler viraria reprovação silenciosa de todo documento.
        if valor not in EvolucaoProntuario.model_fields:
            raise ValueError(f"campo '{valor}' não existe em EvolucaoProntuario.model_fields")
        return valor

    @field_validator("condicao")
    @classmethod
    def _condicao_valida_pela_gramatica_do_motor(cls, valor: dict[str, Any]) -> dict[str, Any]:
        # Mesmo portão que `rules/repository._validar_condicao` aplica na
        # escrita via API: regra de catálogo que o motor não sabe avaliar não
        # pode entrar no banco.
        try:
            CondicaoTypeAdapter.validate_python(valor)
        except ValidationError as exc:
            raise ValueError(f"condicao inválida: {exc}") from exc
        return valor


def carregar_catalogo(nome_arquivo: str) -> list[RegraCatalogo]:
    """Carrega e valida um JSON de `rules/data/`.

    Levanta `ValueError` com o nome do arquivo e o `codigo` da regra ruim
    quando a validação falhar — erro de catálogo precisa dizer qual linha do
    JSON está errada.

    Lê via `importlib.resources`, não caminho relativo a `__file__`: o pacote
    é instalado com `--no-editable` no Dockerfile, e `importlib.resources` é o
    que funciona tanto no wheel quanto no checkout local.
    """
    conteudo = resources.files("homecareos.rules.data").joinpath(nome_arquivo).read_text("utf-8")
    itens = json.loads(conteudo)
    regras: list[RegraCatalogo] = []
    for item in itens:
        codigo = item.get("codigo", "<sem codigo>")
        try:
            regras.append(RegraCatalogo.model_validate(item))
        except ValidationError as exc:
            raise ValueError(f"{nome_arquivo}: regra '{codigo}' inválida: {exc}") from exc
    return regras


def carregar_tiss() -> list[RegraCatalogo]:
    return carregar_catalogo(CATALOGO_TISS)


def carregar_por_operadora() -> dict[str, list[RegraCatalogo]]:
    return {
        codigo_operadora: carregar_catalogo(nome_arquivo)
        for codigo_operadora, nome_arquivo in CATALOGO_POR_OPERADORA.items()
    }
