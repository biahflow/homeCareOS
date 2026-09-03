"""Testes unitários do `SmtpEmailProvider` e da factory — issue #34.

**Nenhuma conexão SMTP.** Toda a conversa com o servidor é interceptada por um
dublê injetado pela `conexao_factory`, que é justamente por que o construtor do
provider aceita uma — ver a docstring dele. O contrato exercitado aqui (STARTTLS
só quando configurado, `login` só quando há usuário, mensagem com
`To`/`From`/`Subject` e o link no corpo) é o que o endpoint de recuperação de
senha depende para funcionar.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from types import TracebackType

import pytest

from homecareos.config import Settings
from homecareos.mailer.errors import EnvioEmailError
from homecareos.mailer.provider import get_email_provider
from homecareos.mailer.smtp import SmtpEmailProvider

HOST = "smtp.teste.local"
PORTA = 587
USUARIO = "sistema@homecareos.local"
# Valor reconhecível de propósito: os testes de vazamento procuram por ele.
SENHA = "senha-smtp-secreta-do-teste"
REMETENTE = "HomeCareOS <sistema@homecareos.local>"
DESTINATARIO = "ana@teste.local"
ASSUNTO = "HomeCareOS — redefinição de senha"
LINK = "http://localhost:3000/redefinir-senha?token=abc123"
CORPO = f"Olá, Ana.\n\nAbra o link:\n\n{LINK}\n"


class ConexaoFalsa:
    """Dublê de `smtplib.SMTP` que registra a sequência de chamadas.

    Registra `starttls`/`login`/`send_message` na ordem em que chegam porque a
    ordem é parte do contrato: `login` antes do `starttls` mandaria a senha em
    claro pela rede.
    """

    def __init__(self, erro: Exception | None = None, erro_no_login: Exception | None = None):
        self.erro = erro
        self.erro_no_login = erro_no_login
        self.chamadas: list[str] = []
        self.credenciais: tuple[str, str] | None = None
        self.mensagens: list[EmailMessage] = []
        self.encerrada = False

    def __enter__(self) -> ConexaoFalsa:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        self.encerrada = True
        return False

    def starttls(self) -> None:
        self.chamadas.append("starttls")

    def login(self, usuario: str, senha: str) -> None:
        self.chamadas.append("login")
        self.credenciais = (usuario, senha)
        if self.erro_no_login is not None:
            raise self.erro_no_login

    def send_message(self, mensagem: EmailMessage) -> None:
        self.chamadas.append("send_message")
        if self.erro is not None:
            raise self.erro
        self.mensagens.append(mensagem)


def _provider(
    conexao: ConexaoFalsa,
    *,
    usuario: str = USUARIO,
    usar_tls: bool = True,
) -> SmtpEmailProvider:
    return SmtpEmailProvider(
        host=HOST,
        porta=PORTA,
        usuario=usuario,
        senha=SENHA,
        remetente=REMETENTE,
        usar_tls=usar_tls,
        conexao_factory=lambda: conexao,  # type: ignore[arg-type]
    )


# --- 1. caminho feliz ----------------------------------------------------------


def test_envio_bem_sucedido_faz_starttls_login_e_manda_a_mensagem_montada() -> None:
    conexao = ConexaoFalsa()

    _provider(conexao).enviar(DESTINATARIO, ASSUNTO, CORPO)

    # `starttls` antes de `login`: a senha não pode sair em claro.
    assert conexao.chamadas == ["starttls", "login", "send_message"]
    assert conexao.credenciais == (USUARIO, SENHA)
    (mensagem,) = conexao.mensagens
    assert mensagem["To"] == DESTINATARIO
    assert mensagem["From"] == REMETENTE
    assert mensagem["Subject"] == ASSUNTO
    assert LINK in mensagem.get_content()
    assert conexao.encerrada


# --- 2. TLS e autenticação são opcionais e independentes -----------------------


def test_sem_tls_nao_chama_starttls() -> None:
    conexao = ConexaoFalsa()

    _provider(conexao, usar_tls=False).enviar(DESTINATARIO, ASSUNTO, CORPO)

    assert conexao.chamadas == ["login", "send_message"]


def test_sem_usuario_nao_chama_login() -> None:
    """Relay interno autenticado por IP: `AUTH` seria recusado por um servidor
    que não pediu autenticação nenhuma."""
    conexao = ConexaoFalsa()

    _provider(conexao, usuario="").enviar(DESTINATARIO, ASSUNTO, CORPO)

    assert conexao.chamadas == ["starttls", "send_message"]
    assert conexao.credenciais is None


# --- 3. falha do servidor vira EnvioEmailError encadeada ----------------------


def test_smtp_exception_vira_envio_email_error_encadeada() -> None:
    causa = smtplib.SMTPRecipientsRefused({DESTINATARIO: (550, b"caixa inexistente")})
    conexao = ConexaoFalsa(erro=causa)

    with pytest.raises(EnvioEmailError) as capturado:
        _provider(conexao).enviar(DESTINATARIO, ASSUNTO, CORPO)

    assert capturado.value.__cause__ is causa
    # Host e porta na mensagem: é o que o operador precisa para saber com qual
    # servidor a aplicação estava falando.
    assert HOST in str(capturado.value)


def test_falha_de_transporte_tambem_vira_envio_email_error() -> None:
    """`OSError` cobre o que não é protocolo: DNS, conexão recusada, timeout."""

    def factory_que_falha() -> smtplib.SMTP:
        raise OSError("connection refused")

    provider = SmtpEmailProvider(
        host=HOST,
        porta=PORTA,
        usuario=USUARIO,
        senha=SENHA,
        remetente=REMETENTE,
        conexao_factory=factory_que_falha,
    )

    with pytest.raises(EnvioEmailError):
        provider.enviar(DESTINATARIO, ASSUNTO, CORPO)


# --- 4. a senha SMTP não vaza --------------------------------------------------


def test_repr_do_provider_nao_contem_a_senha() -> None:
    texto = repr(_provider(ConexaoFalsa()))

    assert SENHA not in texto
    # E ainda diz o que serve para depurar.
    assert HOST in texto
    assert REMETENTE in texto


def test_mensagem_do_erro_nao_contem_a_senha_nem_quando_o_servidor_a_ecoa() -> None:
    """A senha não é interpolada em lugar nenhum do provider; o que este teste
    fecha é o outro caminho — o texto de erro **do servidor** entrar na mensagem
    trazendo a credencial de volta (ver `SmtpEmailProvider._sem_a_senha`)."""
    eco = smtplib.SMTPAuthenticationError(535, f"535 rejeitado: AUTH PLAIN {SENHA}".encode())
    conexao = ConexaoFalsa(erro_no_login=eco)

    with pytest.raises(EnvioEmailError) as capturado:
        _provider(conexao).enviar(DESTINATARIO, ASSUNTO, CORPO)

    assert SENHA not in str(capturado.value)


# --- 5. factory ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "remetente"),
    [("", REMETENTE), (HOST, ""), ("", "")],
)
def test_get_email_provider_devolve_none_sem_host_ou_sem_remetente(
    host: str, remetente: str
) -> None:
    """Recuperação de senha desligada não é falha — ver `mailer/provider.py`."""
    assert get_email_provider(Settings(smtp_host=host, smtp_remetente=remetente)) is None


def test_get_email_provider_devolve_o_provider_smtp_quando_configurado() -> None:
    provider = get_email_provider(Settings(smtp_host=HOST, smtp_remetente=REMETENTE))

    assert isinstance(provider, SmtpEmailProvider)


def test_get_email_provider_nao_exige_usuario_e_senha() -> None:
    """Relay interno autenticado por IP é configuração legítima: exigir
    credencial aqui a tornaria impossível de expressar."""
    provider = get_email_provider(
        Settings(smtp_host=HOST, smtp_remetente=REMETENTE, smtp_usuario="", smtp_senha="")
    )

    assert provider is not None
