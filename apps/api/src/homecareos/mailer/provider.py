"""Porta do gateway de e-mail + factory.

## Por que e-mail, e não o WhatsApp que já está contratado (issue #34)

O gateway uazapi já está configurado e seria mais barato — e mesmo assim a
recuperação de senha sai por e-mail. A razão é o segundo fator: a issue #35
escolheu TOTP, mas enquanto ele não existe é o WhatsApp que serve de canal de
confirmação humana da operação. Usar o mesmo canal para recuperar a senha e
para confirmar quem é a pessoa faria os dois virarem um só: quem tomasse o
WhatsApp de alguém teria, no mesmo movimento, a senha e o fator que deveria
protegê-la. Canais independentes são o ponto inteiro de haver dois.

## Mesmo desenho de `alerts/provider.py`

Protocol + implementação real + factory que decide só a partir da config, e
factory que devolve `None` (e não uma implementação nula) quando não está
configurada. Um `NullProvider` que engolisse o envio faria o endpoint de
"esqueci minha senha" reportar sucesso sem nunca ter mandado nada — e como o
endpoint responde 204 em qualquer caso (ver `auth/router.py`), ninguém
descobriria pela resposta. `None` é o que permite ao endpoint registrar o
`warning` dizendo que a recuperação está desligada.
"""

from __future__ import annotations

from typing import Protocol

from homecareos.config import Settings


class EmailProvider(Protocol):
    """Porta que qualquer gateway de e-mail implementa."""

    def enviar(self, destinatario: str, assunto: str, corpo: str) -> None:
        """Entrega a mensagem. Levanta `EnvioEmailError` quando o gateway recusa."""
        ...


def get_email_provider(settings: Settings) -> EmailProvider | None:
    """`None` quando `smtp_host` ou `smtp_remetente` estão vazios.

    Não é falha: rodar sem SMTP é modo de operação legítimo (ambiente local,
    homologação sem caixa postal). O sistema segue inteiro; só a recuperação de
    senha por autoatendimento fica desligada, e o caminho volta a ser o CLI
    (`python -m homecareos.auth.cli`).

    Host e remetente são os dois campos exigidos porque são os dois sem os quais
    não existe envio: sem host não há para onde conectar, e sem remetente a
    mensagem é recusada por qualquer servidor sério. Usuário e senha ficam de
    fora de propósito — relay interno autenticado por IP é configuração comum e
    não teria como ser expressa se credencial fosse obrigatória.
    """
    if not settings.smtp_host or not settings.smtp_remetente:
        return None

    from homecareos.mailer.smtp import SmtpEmailProvider

    return SmtpEmailProvider(
        host=settings.smtp_host,
        porta=settings.smtp_porta,
        usuario=settings.smtp_usuario,
        senha=settings.smtp_senha,
        remetente=settings.smtp_remetente,
        usar_tls=settings.smtp_usar_tls,
        timeout=settings.smtp_timeout_segundos,
    )
