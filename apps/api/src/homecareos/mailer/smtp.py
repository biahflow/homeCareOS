"""Implementação da porta `EmailProvider` sobre `smtplib` da biblioteca padrão.

O pacote se chama `mailer`, e **não** `email`: `homecareos.email` conviveria com
o módulo `email` da biblioteca padrão que este arquivo importa (`email.message`).
O import absoluto até resolveria para o interpretador; ler o arquivo depois é que
não resolveria.

## A senha SMTP nunca sai daqui

A senha é credencial de envio: quem a tem manda e-mail em nome da empresa — e
e-mail em nome da empresa é o insumo do phishing contra a própria operação. Ela
não pode aparecer em log, `repr` nem mensagem de exceção. Por isso o `__repr__`
abaixo é explícito (mostra host, porta e remetente; omite a senha) e a mensagem
de `EnvioEmailError` passa por `_sem_a_senha` antes de existir: o texto do erro
vem do servidor, e é o único texto de terceiro que entra ali.

## STARTTLS, não SMTPS

`usar_tls` liga `starttls()` sobre a conexão em claro — é o desenho da porta 587,
que é o default. A porta 465 (TLS implícito, `smtplib.SMTP_SSL`) **não** é
suportada nesta entrega; ver a limitação registrada no README.
"""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from email.message import EmailMessage

from homecareos.mailer.errors import EnvioEmailError

# Factory da conexão SMTP. Existe como parâmetro para o teste injetar um dublê
# — ver a docstring do construtor.
ConexaoSmtpFactory = Callable[[], smtplib.SMTP]


class SmtpEmailProvider:
    """Envia texto puro pelo servidor SMTP configurado."""

    def __init__(
        self,
        host: str,
        porta: int,
        usuario: str,
        senha: str,
        remetente: str,
        usar_tls: bool = True,
        timeout: float = 10.0,
        conexao_factory: ConexaoSmtpFactory | None = None,
    ) -> None:
        """`conexao_factory` existe para o teste injetar um dublê de conexão.

        Sem ela, testar o contrato (STARTTLS quando configurado, `login` só
        quando há usuário, mensagem com `To`/`From`/`Subject` e o link no corpo)
        exigiria ou um servidor SMTP de verdade — que é rede, credencial e
        caixa postal em suíte de teste — ou um mock do módulo `smtplib`
        inteiro, que provaria menos. É o mesmo truque do `httpx.Client`
        injetável em `alerts/uazapi.py`.

        A factory é chamada a **cada** envio, e não uma vez no construtor:
        conexão SMTP guardada entre envios morre sozinha por timeout do
        servidor, e o provider é criado por requisição de qualquer forma.
        """
        self._host = host
        self._porta = porta
        self._usuario = usuario
        self._senha = senha
        self._remetente = remetente
        self._usar_tls = usar_tls
        self._timeout = timeout
        self._conexao_factory = conexao_factory if conexao_factory is not None else self._conectar

    def __repr__(self) -> str:
        """Mostra host, porta e remetente e **omite a senha** — ver a docstring do módulo."""
        return (
            f"SmtpEmailProvider(host={self._host!r}, porta={self._porta!r}, "
            f"remetente={self._remetente!r}, senha=<omitida>)"
        )

    def _conectar(self) -> smtplib.SMTP:
        """Conexão real. Construir `smtplib.SMTP` com host já abre o socket."""
        return smtplib.SMTP(self._host, self._porta, timeout=self._timeout)

    def _sem_a_senha(self, texto: str) -> str:
        """Remove a senha do texto, se ela estiver lá.

        Cinto de segurança e não a proteção principal: a senha não é
        interpolada em lugar nenhum desta classe. O que este método cobre é o
        texto que vem do **servidor** — uma resposta de erro que ecoasse a
        linha `AUTH` traria a credencial de volta para dentro da mensagem de
        exceção, que vai para o log.

        O `if` não é redundante: `"".replace("", x)` insere `x` entre cada
        caractere do texto, e senha vazia é configuração válida (relay interno
        autenticado por IP).
        """
        if not self._senha:
            return texto
        return texto.replace(self._senha, "<omitida>")

    def enviar(self, destinatario: str, assunto: str, corpo: str) -> None:
        """Entrega a mensagem. Levanta `EnvioEmailError` em qualquer recusa."""
        mensagem = EmailMessage()
        mensagem["Subject"] = assunto
        mensagem["From"] = self._remetente
        mensagem["To"] = destinatario
        mensagem.set_content(corpo)

        try:
            with self._conexao_factory() as conexao:
                if self._usar_tls:
                    conexao.starttls()
                # Sem usuário não há `login`: relay interno autenticado por IP
                # recusa `AUTH` e o envio morreria na autenticação que ninguém
                # pediu.
                if self._usuario:
                    conexao.login(self._usuario, self._senha)
                conexao.send_message(mensagem)
        except (smtplib.SMTPException, OSError) as exc:
            # `OSError` cobre o que não é protocolo: DNS, conexão recusada,
            # timeout do socket. Encadeada com `from exc` para o traceback
            # preservar a causa; a mensagem, sem a senha, é o que vai para o log.
            raise EnvioEmailError(
                self._sem_a_senha(
                    f"falha ao enviar e-mail por {self._host}:{self._porta}: "
                    f"{type(exc).__name__}: {exc}"
                )
            ) from exc
