"""Papéis, o principal da requisição e os DTOs de `/api/auth`.

`Principal` é o que o resto da aplicação vê depois da autenticação: quem está
fazendo a requisição, sem que nenhum router precise saber se veio cookie de
sessão ou chave de máquina. É `frozen` porque identidade não se corrige no meio
da requisição — um handler que pudesse reescrever o principal reescreveria a
auditoria junto.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

# Rótulo usado em `log_conferencia.usuario` quando quem age é a integração
# máquina-a-máquina autenticada por `X-API-Key`. É o mesmo valor que os routers
# já gravavam antes da issue #30 — o histórico continua legível.
ROTULO_MAQUINA = "api"


class Papel(enum.StrEnum):
    """Os três papéis da matriz aprovada (ADR 0001).

    `conferente` está contido em `coordenador`. `gestor` **não** é superconjunto
    de ninguém: é outro eixo — lê a operação inteira, não a executa, e é o único
    que escreve baseline, que é dado de gestão e não de conferência.
    """

    CONFERENTE = "conferente"
    COORDENADOR = "coordenador"
    GESTOR = "gestor"


@dataclass(frozen=True)
class Principal:
    """Quem está fazendo a requisição: uma pessoa ou uma máquina.

    `rotulo` é o que vai para `log_conferencia.usuario` (o e-mail da pessoa, ou
    `"api"` para máquina); `usuario_id` é o que vai para
    `log_conferencia.usuario_id`, e é `None` para máquina — de propósito: não
    existe pessoa por trás da chave de integração, e forjar um id ali faria a
    auditoria apontar para alguém que não fez nada.
    """

    tipo: Literal["usuario", "maquina"]
    usuario_id: uuid.UUID | None
    papel: Papel | None
    rotulo: str


class LoginRequest(BaseModel):
    """Corpo de `POST /api/auth/login`.

    `email` é `str` e não `EmailStr` por dois motivos: `EmailStr` exigiria a
    dependência `email-validator`, e o login não é cadastro — recusar por
    formato de e-mail aqui só criaria uma resposta distinguível ("e-mail
    inválido") no lugar do 401 idêntico que o login deve dar em toda falha.

    `senha` é `str` comum e não um tipo com `repr` mascarado por escolha
    consciente de não criar falsa sensação de segurança: o que impede a senha de
    vazar é ela nunca entrar em log, resposta ou coluna — não o `repr` do DTO.
    """

    email: str = Field(min_length=1)
    senha: str = Field(min_length=1)


class EsqueciSenhaRequest(BaseModel):
    """Corpo de `POST /api/auth/senha/esqueci`.

    `str` e não `EmailStr` pelo mesmo motivo de `LoginRequest`, e aqui ele é
    ainda mais forte: recusar por formato criaria uma resposta distinguível
    ("e-mail inválido") no endpoint cujo contrato inteiro é responder **igual**
    para qualquer entrada.
    """

    email: str = Field(min_length=1)


class RedefinirSenhaRequest(BaseModel):
    """Corpo de `POST /api/auth/senha/redefinir`.

    `nova_senha` **não** declara `min_length` de propósito: o piso de tamanho é
    de `senhas.validar_forca`, configurável por `SENHA_MINIMA_CARACTERES`. Um
    `min_length` aqui seria a mesma regra em dois lugares, respondendo com duas
    mensagens diferentes conforme o valor caísse de um lado ou do outro.
    """

    token: str = Field(min_length=1)
    nova_senha: str


class UsuarioOut(BaseModel):
    """Usuário como a API o devolve. Não existe campo de senha aqui, e nunca vai existir."""

    id: uuid.UUID
    nome: str
    email: str
    papel: Papel
    ativo: bool

    model_config = {"from_attributes": True}


class MaquinaOut(BaseModel):
    """Resposta de `GET /api/auth/eu` para a integração máquina-a-máquina.

    Um usuário forjado seria pior que esta resposta magra: o frontend leria um
    nome e um papel que não existem, e a tela mostraria uma pessoa inexistente
    como autora do que a chave de API fez.
    """

    tipo: Literal["maquina"] = "maquina"
