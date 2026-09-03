"""Erros da trilha de e-mail.

Uma família só, e é o suficiente: diferente da trilha de alertas — que separa
erro de configuração (`AlertConfigError`) de recusa do gateway (`EnvioError`)
porque as duas pedem ação de gente diferente —, aqui não existe configuração
inválida a reportar. Configuração incompleta não é erro: `get_email_provider`
devolve `None` e a recuperação de senha fica desligada (ver `mailer/provider.py`).
"""

from __future__ import annotations


class EnvioEmailError(Exception):
    """O servidor SMTP não entregou a mensagem.

    A mensagem desta exceção pode ir para o log da aplicação e por isso **nunca**
    carrega a senha SMTP — ver a docstring de `mailer/smtp.py`.
    """
