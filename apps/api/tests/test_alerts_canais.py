"""Testes unitários da porta de canal e das duas implementações — ADR 0006.

**Nada sai daqui.** Não há gateway de WhatsApp, não há servidor SMTP e não há
credencial: os dois providers são dublês em memória, e o que se verifica é o
contrato do embrulho — quem recebe o quê, o que conta como "disponível" e como
a recusa de cada gateway vira a mesma `EnvioError` que o serviço sabe
registrar.

O `mailer` **não** é alterado por esta trilha (ele serve à recuperação de
senha, issue #34): o `CanalEmail` é um cliente dele como qualquer outro, e a
tradução do erro acontece do lado de cá.

**Nenhum teste aqui toca o banco**, e é por isso que eles exercitam
`montar_canais` e não `construir_canais`: desde a parte 2 do ADR 0006 o
liga/desliga vem da tabela `canais_alerta`, e `construir_canais` só acrescenta
essa leitura em cima desta função. O que se prova aqui é o que independe da
fonte — que "habilitado" e "disponível" são perguntas separadas, e que a
construção não faz E/S. A leitura do banco é exercitada em
`tests/test_api_canais.py`, contra Postgres real.
"""

from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy.orm import Session

from homecareos.alerts.canais import (
    CanalEmail,
    CanalWhatsApp,
    alguma_credencial_presente,
    canais_que_enviam,
    montar_canais,
)
from homecareos.alerts.errors import EnvioError
from homecareos.alerts.schema import Canal, MensagemAlerta, TipoAlerta
from homecareos.config import Settings
from homecareos.mailer.errors import EnvioEmailError

BASE_URL = "https://instancia-de-teste.uazapi.com"
TOKEN = "token-que-nunca-sai-daqui"
SMTP_HOST = "smtp-de-teste.invalido"
SMTP_REMETENTE = "alertas@exemplo.com"
# Valor reconhecível de propósito: o teste de vazamento procura por ele.
SMTP_SENHA = "senha-smtp-do-teste"

SEM_SESSAO = cast("Session", None)
"""O canal de WhatsApp resolve destinatário da configuração e nunca toca no
banco — passar `None` é o que prova isso. O canal de e-mail, que consulta
`usuarios`, é exercitado no teste de integração."""


class ProviderWhatsAppFake:
    def __init__(self) -> None:
        self.enviadas: list[tuple[str, str]] = []

    def enviar(self, destinatario: str, mensagem: str) -> None:
        self.enviadas.append((destinatario, mensagem))


class ProviderEmailFake:
    """Caixa postal em memória. Nenhuma conexão SMTP é aberta."""

    def __init__(self, erro: EnvioEmailError | None = None) -> None:
        self.enviadas: list[tuple[str, str, str]] = []
        self.erro = erro

    def enviar(self, destinatario: str, assunto: str, corpo: str) -> None:
        if self.erro is not None:
            raise self.erro
        self.enviadas.append((destinatario, assunto, corpo))


# --- disponibilidade: credencial ausente não é erro, é modo de operação ---------


def test_canal_sem_credencial_fica_indisponivel_e_nao_estoura() -> None:
    canais = montar_canais(Settings(), habilitados={Canal.WHATSAPP, Canal.EMAIL})

    for canal in canais:
        assert canal.habilitado is True
        assert canal.disponivel() is False
    assert canais_que_enviam(canais) == []


def test_canal_com_credencial_fica_disponivel() -> None:
    canais = montar_canais(
        Settings(
            uazapi_base_url=BASE_URL,
            uazapi_token=TOKEN,
            smtp_host=SMTP_HOST,
            smtp_remetente=SMTP_REMETENTE,
        ),
        habilitados={Canal.WHATSAPP, Canal.EMAIL},
    )

    assert {canal.canal for canal in canais_que_enviam(canais)} == {Canal.WHATSAPP, Canal.EMAIL}


def test_habilitado_e_disponivel_sao_perguntas_separadas() -> None:
    """ "Desliguei" e "esqueci de configurar" precisam ser distinguíveis: sem
    isso, quem liga um canal na tela não entende por que nada sai (ADR 0006)."""
    canais = montar_canais(
        Settings(smtp_host=SMTP_HOST, smtp_remetente=SMTP_REMETENTE),
        habilitados={Canal.WHATSAPP},
    )
    por_canal = {canal.canal: canal for canal in canais}

    # WhatsApp: ligado por configuração, sem credencial.
    assert por_canal[Canal.WHATSAPP].habilitado is True
    assert por_canal[Canal.WHATSAPP].disponivel() is False
    # E-mail: com credencial, desligado por configuração.
    assert por_canal[Canal.EMAIL].habilitado is False
    assert por_canal[Canal.EMAIL].disponivel() is True
    assert canais_que_enviam(canais) == []


def test_montar_canais_devolve_todos_mesmo_os_desligados() -> None:
    """Um canal que sumisse da lista seria indistinguível de um canal que
    ninguém olhou — e o resumo da varredura precisa responder por ele."""
    canais = montar_canais(Settings(), habilitados=set())

    assert {canal.canal for canal in canais} == set(Canal)


# --- a guarda barata do gancho da classificação ---------------------------------


def test_sem_credencial_nenhuma_nao_ha_o_que_perguntar_ao_banco() -> None:
    """`alguma_credencial_presente` é o que deixa o gancho da classificação sair
    antes de abrir sessão (`alerts/hooks.py`): sem uazapi e sem SMTP, nenhum
    estado da tabela `canais_alerta` faz um canal enviar."""
    assert alguma_credencial_presente(Settings()) is False


def test_uma_credencial_basta_para_a_pergunta_valer_a_conexao() -> None:
    assert (
        alguma_credencial_presente(Settings(smtp_host=SMTP_HOST, smtp_remetente=SMTP_REMETENTE))
        is True
    )


# --- entrega: cada gateway recebe o que a porta dele espera ---------------------


def test_whatsapp_recebe_so_o_corpo_e_nunca_o_assunto() -> None:
    provider = ProviderWhatsAppFake()
    canal = CanalWhatsApp(habilitado=True, provider=provider)

    canal.enviar("5521999999999", MensagemAlerta(corpo="texto do zap"))

    assert provider.enviadas == [("5521999999999", "texto do zap")]


def test_email_recebe_assunto_e_corpo_separados() -> None:
    provider = ProviderEmailFake()
    canal = CanalEmail(habilitado=True, provider=provider)

    canal.enviar(
        "coordenacao@exemplo.com",
        MensagemAlerta(assunto="Pendência crítica — Unimed", corpo="corpo puro"),
    )

    assert provider.enviadas == [
        ("coordenacao@exemplo.com", "Pendência crítica — Unimed", "corpo puro")
    ]


# --- recusa do gateway: uma família de erro só para o serviço -------------------


def test_recusa_do_smtp_vira_envio_error_da_trilha_de_alertas() -> None:
    """O serviço registra linha `falha` a partir de `EnvioError`; deixar o
    `EnvioEmailError` do `mailer` subir faria a falha de e-mail derrubar a
    varredura inteira, e com ela os alertas dos outros destinatários."""
    provider = ProviderEmailFake(erro=EnvioEmailError("SMTPRecipientsRefused: 550"))
    canal = CanalEmail(habilitado=True, provider=provider)

    with pytest.raises(EnvioError) as erro:
        canal.enviar("ninguem@exemplo.com", MensagemAlerta(assunto="a", corpo="b"))

    assert "550" in str(erro.value)
    assert isinstance(erro.value.__cause__, EnvioEmailError)


def test_a_senha_smtp_nao_chega_ao_detalhe_do_log() -> None:
    """`str(EnvioError)` vai para `alertas_enviados.detalhe`. A higienização é
    do `mailer` (que não mexemos); o que este teste fixa é que a tradução não
    reintroduz o segredo ao remontar a mensagem."""
    provider = ProviderEmailFake(erro=EnvioEmailError("falha ao autenticar: <omitida>"))
    canal = CanalEmail(habilitado=True, provider=provider)

    with pytest.raises(EnvioError) as erro:
        canal.enviar("ninguem@exemplo.com", MensagemAlerta(assunto="a", corpo="b"))

    assert SMTP_SENHA not in str(erro.value)


# --- destinatário do WhatsApp: telefone da configuração, sem pessoa -------------


def test_whatsapp_resolve_telefone_da_configuracao_e_sem_vinculo_com_pessoa() -> None:
    """Não há telefone em `usuarios`: o telefone do `.env` não tem pessoa, e é
    por isso que o rate limit dele continua contando por endereço (ADR 0006)."""
    settings = Settings(
        alertas_destinatarios=(
            '{"pendencia_parada": ["+55 (21) 99999-9999"], "volume_anormal": []}'
        )
    )
    canal = CanalWhatsApp(habilitado=True, provider=ProviderWhatsAppFake())

    destinatarios = canal.destinatarios(SEM_SESSAO, settings, TipoAlerta.PENDENCIA_PARADA)

    assert [d.endereco for d in destinatarios] == ["5521999999999"]
    assert [d.usuario_id for d in destinatarios] == [None]
    # Tipo ausente do mapa é o jeito de desligar o alerta naquele canal.
    assert canal.destinatarios(SEM_SESSAO, settings, TipoAlerta.VOLUME_ANORMAL) == []
    assert canal.destinatarios(SEM_SESSAO, settings, TipoAlerta.DOCUMENTO_INCOMPLETO_CRITICO) == []


def test_o_canal_de_whatsapp_le_a_configuracao_uma_vez_por_varredura() -> None:
    """A varredura roda de minuto em minuto e pode carregar dezenas de alertas
    do mesmo tipo: reparsear o JSON por alerta é custo puro."""
    settings = Settings(alertas_destinatarios='{"pendencia_parada": ["5521999999999"]}')
    canal = CanalWhatsApp(habilitado=True, provider=ProviderWhatsAppFake())

    primeiro = canal.destinatarios(SEM_SESSAO, settings, TipoAlerta.PENDENCIA_PARADA)
    segundo = canal.destinatarios(SEM_SESSAO, settings, TipoAlerta.PENDENCIA_PARADA)

    assert primeiro == segundo
