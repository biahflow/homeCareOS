"""Testes do comando de fumaça de SMTP: `python -m homecareos.mailer.cli`.

**Nenhum teste envia e-mail de verdade.** `get_email_provider` é sempre
monkeypatchado no módulo do CLI (mesmo padrão de `monkeypatch.setattr(modulo,
"get_settings", ...)` já usado em `test_migrations.py` e
`test_retencao.py`) — para o cenário de falha de envio, o dublê é o
`SmtpEmailProvider` real com uma `conexao_factory` falsa (o mesmo truque de
`test_mailer_smtp.py`), porque o objetivo ali é provar que o mascaramento de
senha do provider real (`_sem_a_senha`) é o que chega até o operador, não
reimplementar essa lógica no teste.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from types import TracebackType

import pytest

from homecareos.config import Settings
from homecareos.mailer import cli as mailer_cli
from homecareos.mailer.errors import EnvioEmailError
from homecareos.mailer.smtp import SmtpEmailProvider

DESTINATARIO = "ana@teste.local"
SMTP_HOST = "smtp.teste.local"
SMTP_PORTA = 2525
SMTP_REMETENTE = "HomeCareOS <sistema@homecareos.local>"
SMTP_USUARIO = "sistema@homecareos.local"
# Valor reconhecível de propósito: os testes de vazamento procuram por ele.
SENHA = "senha-smtp-secreta-do-teste"


class _ProviderFalso:
    """Dublê mínimo de `EmailProvider`: registra a chamada e não fala com rede
    nenhuma."""

    def __init__(self, erro: Exception | None = None) -> None:
        self.erro = erro
        self.chamadas: list[tuple[str, str, str]] = []

    def enviar(self, destinatario: str, assunto: str, corpo: str) -> None:
        self.chamadas.append((destinatario, assunto, corpo))
        if self.erro is not None:
            raise self.erro


class _ConexaoQueEcoaSenha:
    """Dublê de `smtplib.SMTP` cujo `login` falha ecoando a senha na mensagem
    do servidor — o cenário exato que `SmtpEmailProvider._sem_a_senha` existe
    para cobrir (ver `test_mailer_smtp.py`)."""

    def __enter__(self) -> _ConexaoQueEcoaSenha:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return False

    def starttls(self) -> None:
        pass

    def login(self, usuario: str, senha: str) -> None:
        raise smtplib.SMTPAuthenticationError(535, f"535 rejeitado: AUTH PLAIN {senha}".encode())

    def send_message(self, mensagem: EmailMessage) -> None:
        raise AssertionError("não deveria chegar a enviar com login recusado")


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "smtp_host": SMTP_HOST,
        "smtp_porta": SMTP_PORTA,
        "smtp_remetente": SMTP_REMETENTE,
        "smtp_usuario": SMTP_USUARIO,
        "smtp_senha": SENHA,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --- 1. provider None: SMTP desligado ------------------------------------------


def test_provider_none_sai_com_codigo_1_e_stderr_nomeia_o_campo_que_falta(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings_sem_host = Settings(smtp_host="", smtp_remetente=SMTP_REMETENTE)
    monkeypatch.setattr(mailer_cli, "get_settings", lambda: settings_sem_host)
    monkeypatch.setattr(mailer_cli, "get_email_provider", lambda settings: None)
    # Sem dublê de envio nenhum: se o código tentasse enviar, não haveria
    # provider para chamar e o teste estouraria com AttributeError antes de
    # qualquer asserção — a própria ausência de dublê já prova "nada é enviado".

    codigo = mailer_cli.main(["--para", DESTINATARIO])

    saida = capsys.readouterr()
    assert codigo == 1
    assert "SMTP_HOST" in saida.err
    assert "SMTP_REMETENTE" not in saida.err
    assert saida.out == ""


def test_provider_none_com_os_dois_campos_vazios_nomeia_os_dois(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        mailer_cli, "get_settings", lambda: Settings(smtp_host="", smtp_remetente="")
    )
    monkeypatch.setattr(mailer_cli, "get_email_provider", lambda settings: None)

    codigo = mailer_cli.main(["--para", DESTINATARIO])

    erro = capsys.readouterr().err
    assert codigo == 1
    assert "SMTP_HOST" in erro
    assert "SMTP_REMETENTE" in erro


# --- 2. sucesso -----------------------------------------------------------------


def test_envio_bem_sucedido_sai_0_e_stdout_traz_host_porta_remetente_destinatario(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = _ProviderFalso()
    monkeypatch.setattr(mailer_cli, "get_settings", lambda: _settings())
    monkeypatch.setattr(mailer_cli, "get_email_provider", lambda settings: provider)

    codigo = mailer_cli.main(["--para", DESTINATARIO])

    saida = capsys.readouterr()
    assert codigo == 0
    assert provider.chamadas == [(DESTINATARIO, mailer_cli.ASSUNTO_PADRAO, mailer_cli.CORPO_PADRAO)]
    assert SMTP_HOST in saida.out
    assert str(SMTP_PORTA) in saida.out
    assert SMTP_REMETENTE in saida.out
    assert DESTINATARIO in saida.out
    assert "autenticado com usuário: sim" in saida.out
    # A senha nunca aparece, nem no caminho feliz.
    assert SENHA not in saida.out
    assert SENHA not in saida.err


def test_envio_bem_sucedido_sem_usuario_diz_que_nao_autenticou(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = _ProviderFalso()
    monkeypatch.setattr(mailer_cli, "get_settings", lambda: _settings(smtp_usuario=""))
    monkeypatch.setattr(mailer_cli, "get_email_provider", lambda settings: provider)

    codigo = mailer_cli.main(["--para", DESTINATARIO])

    assert codigo == 0
    assert "autenticado com usuário: não" in capsys.readouterr().out


def test_assunto_e_corpo_customizados_sao_repassados_ao_provider(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = _ProviderFalso()
    monkeypatch.setattr(mailer_cli, "get_settings", lambda: _settings())
    monkeypatch.setattr(mailer_cli, "get_email_provider", lambda settings: provider)

    codigo = mailer_cli.main(
        ["--para", DESTINATARIO, "--assunto", "Assunto custom", "--corpo", "Corpo custom"]
    )

    assert codigo == 0
    assert provider.chamadas == [(DESTINATARIO, "Assunto custom", "Corpo custom")]


# --- 3. falha de envio (EnvioEmailError) -----------------------------------------


def test_envio_email_error_sai_com_codigo_1_e_mensagem_do_servidor_no_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mensagem_servidor = f"falha ao enviar e-mail por {SMTP_HOST}:{SMTP_PORTA}: recusado"
    provider = _ProviderFalso(erro=EnvioEmailError(mensagem_servidor))
    monkeypatch.setattr(mailer_cli, "get_settings", lambda: _settings())
    monkeypatch.setattr(mailer_cli, "get_email_provider", lambda settings: provider)

    codigo = mailer_cli.main(["--para", DESTINATARIO])

    saida = capsys.readouterr()
    assert codigo == 1
    assert SMTP_HOST in saida.err
    assert "recusado" in saida.err
    assert saida.out == ""


# --- 4. a senha nunca aparece, nem quando o servidor a ecoa ----------------------


def test_falha_de_login_que_ecoa_a_senha_chega_mascarada_ate_o_operador(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Usa o `SmtpEmailProvider` REAL (com conexão falsa injetada) para provar
    que o caminho do CLI passa por `_sem_a_senha`, e não reimplementa o
    mascaramento — ver a docstring do módulo de teste."""
    provider = SmtpEmailProvider(
        host=SMTP_HOST,
        porta=SMTP_PORTA,
        usuario=SMTP_USUARIO,
        senha=SENHA,
        remetente=SMTP_REMETENTE,
        conexao_factory=lambda: _ConexaoQueEcoaSenha(),  # type: ignore[arg-type,return-value]
    )
    monkeypatch.setattr(mailer_cli, "get_settings", lambda: _settings())
    monkeypatch.setattr(mailer_cli, "get_email_provider", lambda settings: provider)

    codigo = mailer_cli.main(["--para", DESTINATARIO])

    saida = capsys.readouterr()
    assert codigo == 1
    assert SENHA not in saida.err
    assert SENHA not in saida.out
    # A mensagem do servidor continua chegando, só sem a senha.
    assert "535" in saida.err


# --- 5. --para é obrigatório ------------------------------------------------------


def test_para_ausente_e_erro_de_argumento(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        mailer_cli.main([])

    assert excinfo.value.code == 2
    assert "--para" in capsys.readouterr().err
