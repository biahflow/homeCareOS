"""Segundo fator por TOTP e códigos de recuperação — issue #35.

TOTP em app autenticador (Google Authenticator, Authy, 1Password), e não código
por WhatsApp ou SMS: funciona offline, não depende do gateway de mensagens estar
no ar, não custa mensagem, é imune a SIM swap, e mantém o fator separado do
canal de recuperação de senha (issue #34, e-mail) — um e-mail comprometido não
entrega os dois.

`pyotp` entra só para derivar o código de um passo (`TOTP.at`) e montar a URI
`otpauth://`. A janela de aceitação e o anti-replay são escritos aqui, à mão,
porque `TOTP.verify` devolve **booleano** e o que precisamos guardar é o *passo*
aceito — ver `verificar_codigo`.

Duas escolhas que não são estilo:

- **`hmac.compare_digest`, nunca `==`.** É comparação de credencial. O `==` de
  string curta vaza o prefixo certo por tempo de resposta, e são só seis
  dígitos.
- **O segredo fica em claro no banco**, e a limitação é declarada em vez de
  maquiada: não há KMS neste projeto, e "criptografar" com uma chave guardada no
  mesmo `.env` que acompanha o dump seria teatro. Ver a migration
  `e1f4a7c92b58` e o README.
"""

from __future__ import annotations

import hmac
import secrets
import uuid
from datetime import UTC, datetime

import pyotp
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from homecareos.auth import senhas
from homecareos.db.models import CodigoRecuperacaoMfa

# Duração de um passo TOTP, em segundos. 30 é o default do RFC 6238 e é o que
# todo app autenticador assume — não é configurável de propósito: mudar isto
# invalidaria em silêncio todos os segredos já cadastrados, e cada pessoa
# descobriria pelo login que parou de funcionar.
PASSO_SEGUNDOS = 30

# Quantos dígitos o código tem. Também default do RFC e de todo app.
DIGITOS = 6


def gerar_segredo() -> str:
    """Segredo TOTP novo, em base32 — o formato que o app autenticador lê."""
    return pyotp.random_base32()


def uri_otpauth(segredo: str, *, email: str, emissor: str) -> str:
    """URI `otpauth://totp/...` que vira o QR code do cadastro.

    O `emissor` é o nome que aparece na lista de contas do app autenticador, e
    o `email` é o que distingue duas contas do mesmo sistema no mesmo celular —
    sem ele, quem tem conta de teste e conta real vê duas entradas idênticas e
    escolhe no chute.
    """
    return pyotp.TOTP(segredo).provisioning_uri(name=email, issuer_name=emissor)


def verificar_codigo(
    segredo: str,
    codigo: str,
    *,
    agora: datetime,
    janela: int,
    ultimo_passo: int | None,
) -> int | None:
    """Devolve o **passo** aceito, ou `None`. Nunca levanta.

    Devolve o passo, e não um booleano, porque é ele o anti-replay: quem chama
    **precisa** gravá-lo em `usuarios.mfa_ultimo_passo`. Sem isso, o mesmo
    código de seis dígitos vale durante toda a janela de tolerância, e quem o
    interceptar (ombro, captura de tela, malware de teclado) tem ~90 segundos
    para reusá-lo — o segundo fator viraria um código de uso múltiplo.

    `ultimo_passo` recusa o que já foi usado **e tudo que veio antes**: aceitar
    um passo anterior ao último reabriria a janela para trás, que é a mesma
    falha por outro lado.

    `janela` é a tolerância para relógio dessincronizado (1 = passo anterior e
    seguinte, ±30s). Código malformado — letras, tamanho errado, vazio, dígito
    não-ASCII — devolve `None` sem levantar: a entrada vem de quem chama a API,
    e um 500 aqui seria resposta distinguível de um 401.
    """
    candidato = codigo.strip()
    # `isascii()` junto com `isdigit()` não é preciosismo: `isdigit()` aceita
    # dígito arábico-índico ("١٢٣٤٥٦"), e `hmac.compare_digest` levanta
    # `TypeError` com `str` fora de ASCII — o mesmo cuidado que `api/auth.py`
    # documenta para a chave de máquina.
    if len(candidato) != DIGITOS or not candidato.isascii() or not candidato.isdigit():
        return None

    totp = pyotp.TOTP(segredo)
    passo_atual = int(agora.timestamp()) // PASSO_SEGUNDOS
    for delta in range(-janela, janela + 1):
        passo = passo_atual + delta
        if hmac.compare_digest(totp.at(passo * PASSO_SEGUNDOS), candidato):
            if ultimo_passo is not None and passo <= ultimo_passo:
                return None
            return passo
    return None


def gerar_codigos_recuperacao(quantidade: int) -> list[str]:
    """`quantidade` códigos distintos, em claro. Quem chama os hasheia e os
    mostra **uma única vez**.

    `secrets.token_hex(5)` — 40 bits de um gerador criptográfico — formatado em
    dois blocos (`a1b2c-3d4e5`) porque alguém vai copiar isto à mão de uma tela
    para um papel, e bloco de cinco é o que se confere sem perder a conta.

    A distinção entre eles é garantida no laço, e não deixada para a
    probabilidade: colisão em 40 bits é improbabilíssima, mas dois códigos
    iguais na mesma lista fariam o segundo parecer "já usado" logo depois do
    primeiro — um bug que só aparece na pior hora e não se reproduz.
    """
    codigos: list[str] = []
    while len(codigos) < quantidade:
        bruto = secrets.token_hex(5)
        codigo = f"{bruto[:5]}-{bruto[5:]}"
        if codigo not in codigos:
            codigos.append(codigo)
    return codigos


def normalizar_codigo_recuperacao(codigo: str) -> str:
    """Forma canônica do código digitado — na geração e na conferência.

    Espaço nas pontas e maiúsculas são erro de digitação, não código diferente:
    quem copiou de um papel escreveu `A1B2C-3D4E5` metade das vezes. O hífen faz
    parte do código como ele é mostrado, e continua obrigatório.
    """
    return codigo.strip().lower()


def consumir_codigo_recuperacao(session: DbSession, usuario_id: uuid.UUID, codigo: str) -> bool:
    """Marca `used_at` no código que casar e devolve `True`. **Não commita.**

    Não commita porque a marcação entra na mesma transação que completa a
    sessão pendente (ver `auth/router.py`): as duas entram juntas ou não entram.
    Um commit aqui queimaria o código de recuperação de alguém sem completar o
    login dessa pessoa — e ela tem uma lista finita deles.

    As linhas são travadas com `FOR UPDATE` até o fim da transação, pela mesma
    razão do token de recuperação de senha (`auth/recuperacao.consumir_token`):
    "uso único" precisa ser verdade sob concorrência. Sem a trava, duas
    verificações simultâneas com o mesmo código leem `used_at is None` as duas
    antes de qualquer uma marcar, e as duas passam.

    A varredura é linear sobre os códigos **não usados** da pessoa, com um
    Argon2 por linha, e é aceitável: são poucos por conta
    (`MFA_CODIGOS_RECUPERACAO`, 8), e este caminho só é alcançado depois de o
    código TOTP já ter falhado.

    `used_at` recebe `datetime.now(UTC)` em vez de um `agora` de quem chama —
    ao contrário do resto do módulo de auth — porque aqui ele é dado de
    auditoria: nada compara este instante para decidir validade.
    """
    candidato = normalizar_codigo_recuperacao(codigo)
    linhas = (
        session.scalars(
            select(CodigoRecuperacaoMfa)
            .where(
                CodigoRecuperacaoMfa.usuario_id == usuario_id,
                CodigoRecuperacaoMfa.used_at.is_(None),
            )
            .with_for_update()
        )
        .unique()
        .all()
    )
    for linha in linhas:
        if senhas.verificar(linha.codigo_hash, candidato):
            linha.used_at = datetime.now(UTC)
            return True
    return False
