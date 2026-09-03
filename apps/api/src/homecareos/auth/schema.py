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


class MfaPendenteOut(BaseModel):
    """Resposta de `POST /api/auth/login` quando a conta tem MFA ativado.

    **Não carrega dado nenhum do usuário**, e é o ponto: quem apresentou só a
    senha ainda não provou quem é. Devolver nome, papel e id aqui entregaria
    metade da conta a quem parou no primeiro fator — e o frontend passaria a
    poder desenhar a tela logada antes do segundo passo.
    """

    mfa_pendente: Literal[True] = True


class MfaVerificarRequest(BaseModel):
    """Corpo de `POST /api/auth/mfa/verificar`.

    `codigo` aceita tanto os seis dígitos do app autenticador quanto um código
    de recuperação (`a1b2c-3d4e5`): é um campo só porque, para quem digita, é a
    mesma pergunta — e dois campos separados diriam a quem sonda qual dos dois
    caminhos falhou.
    """

    codigo: str = Field(min_length=1)


class MfaIniciarOut(BaseModel):
    """Resposta de `POST /api/auth/mfa/iniciar`: o segredo e a URI do QR code.

    O segredo sai em claro **uma vez**, para quem já está autenticado e está
    cadastrando o próprio app. `otpauth_uri` é o mesmo segredo no formato que
    vira QR code — quem tem o app em outro aparelho digita o `secret` à mão.
    """

    secret: str
    otpauth_uri: str


class MfaConfirmarRequest(BaseModel):
    """Corpo de `POST /api/auth/mfa/confirmar`: o primeiro código gerado pelo app.

    Confirmar com um código é o que prova que o app guardou o segredo. Ativar
    sem essa prova trancaria para fora quem errasse o cadastro do QR code.
    """

    codigo: str = Field(min_length=1)


class MfaCodigosRecuperacaoOut(BaseModel):
    """Os códigos de recuperação, em claro. Esta é a **única** vez que eles existem.

    O banco guarda só o hash Argon2id (ver
    `db/models/codigo_recuperacao_mfa.py`): não há endpoint que os mostre de
    novo, e quem os perder junto com o celular precisa de alguém que administre
    o banco. É o preço de não guardar credencial em claro.
    """

    codigos: list[str]


class MfaDesativarRequest(BaseModel):
    """Corpo de `POST /api/auth/mfa/desativar`: senha **e** código atual.

    Os dois, e não um: com só o código, uma sessão sequestrada desligaria o
    segundo fator sozinha — que é exatamente o que ele existe para impedir. E a
    senha sozinha não bastaria porque ela pode ter vazado, que é a hipótese que
    faz alguém ativar MFA.
    """

    senha: str = Field(min_length=1)
    codigo: str = Field(min_length=1)


class MaquinaOut(BaseModel):
    """Resposta de `GET /api/auth/eu` para a integração máquina-a-máquina.

    Um usuário forjado seria pior que esta resposta magra: o frontend leria um
    nome e um papel que não existem, e a tela mostraria uma pessoa inexistente
    como autora do que a chave de API fez.
    """

    tipo: Literal["maquina"] = "maquina"
