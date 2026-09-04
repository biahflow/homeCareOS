"""Canal de alerta: a porta comum que o serviço conhece, e as duas implementações.

ADR 0006. Antes desta entrega, `alerts/service._despachar_para` recebia um
`WhatsAppProvider` e chamava `provider.enviar(destinatario, mensagem)`. Um
segundo canal não cabe ali: as duas portas do projeto **divergem na
assinatura** e uma delas não é nossa para mudar.

    WhatsAppProvider.enviar(destinatario, mensagem)
    EmailProvider.enviar(destinatario, assunto, corpo)   # mailer/, da recuperação de senha

## O que um canal é, e por que é isto e não menos

Um canal responde às cinco perguntas que fazem um alerta sair, e as cinco
respostas são **diferentes por canal** — é isso que o torna um conceito, e não
um `if` no serviço:

| pergunta | WhatsApp | e-mail |
| --- | --- | --- |
| está ligado? | `ALERTAS_CANAIS` (banco, na parte 2 do ADR) | idem |
| tem credencial? | uazapi (base URL + token) | SMTP (host + remetente) |
| para quem? | telefones de `ALERTAS_DESTINATARIOS` | e-mails das contas **ativas** do papel |
| que texto? | emoji e `*negrito*` | texto puro, com **assunto** |
| como entrega? | `WhatsAppProvider` | `EmailProvider` do `mailer` |

O serviço, depois disto, não sabe o que é um telefone nem o que é um assunto:
ele itera canais.

## O `mailer` não é alterado, é embrulhado

`mailer/` serve à recuperação de senha e tem contrato próprio (issue #34) — o
`CanalEmail` chama `EmailProvider.enviar` como qualquer outro cliente e traduz
`EnvioEmailError` para o `EnvioError` da trilha de alertas, que é o que o
serviço sabe registrar como linha `falha`. A tradução preserva a mensagem já
higienizada por `mailer/smtp.py`: a senha SMTP nunca aparece nela, e essa
mensagem vai para `alertas_enviados.detalhe`.

## Habilitado e disponível são duas perguntas

    canal habilitado (configuração)  x  credencial presente (.env)  =  canal envia

Manter as duas separadas é o que resolve a ambiguidade que existe hoje na
recuperação de senha, onde "desligado porque decidi" e "desligado porque não
configurei" são indistinguíveis e a única pista é uma linha de log. O
`ResumoVarredura` devolve as duas (`ResumoVarredura.canais`), e a tela da parte
2 depende disso para não deixar alguém ligar um canal e não entender por que
nada sai.

## O que a parte 2 do ADR troca aqui

`construir_canais` lê o liga/desliga de `ALERTAS_CANAIS`. A parte 2 o lê de uma
tabela de canais editável pelo coordenador, com a mudança auditada. **Só esta
função muda**: o `habilitado` já é um dado do canal, e nem o serviço, nem os
templates, nem o log perguntam de onde ele veio.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from homecareos.alerts import config, repository, templates
from homecareos.alerts.errors import EnvioError
from homecareos.alerts.provider import WhatsAppProvider, get_provider
from homecareos.alerts.schema import Canal, Destinatario, MensagemAlerta, TipoAlerta
from homecareos.config import Settings
from homecareos.mailer.errors import EnvioEmailError
from homecareos.mailer.provider import EmailProvider, get_email_provider


class CanalAlerta(Protocol):
    """A porta comum. Uma instância vale por uma varredura — ver `_memoria`."""

    canal: Canal
    habilitado: bool
    """Decisão de quem opera: este canal deve enviar? Vem de configuração hoje e
    de banco na parte 2 do ADR 0006."""

    def disponivel(self) -> bool:
        """Há credencial para enviar? Ausência **não é erro**, é modo de operação."""
        ...

    def destinatarios(
        self, session: Session, settings: Settings, tipo: TipoAlerta
    ) -> list[Destinatario]:
        """Quem recebe este tipo de alerta por este canal. Lista vazia desliga o tipo."""
        ...

    def renderizar(
        self, tipo: TipoAlerta, contexto: dict[str, str], settings: Settings
    ) -> MensagemAlerta:
        """O texto do alerta escrito para ESTE canal."""
        ...

    def enviar(self, endereco: str, mensagem: MensagemAlerta) -> None:
        """Entrega. Levanta `EnvioError` quando o gateway recusa."""
        ...


class CanalWhatsApp:
    """Embrulha o `WhatsAppProvider` que já existe — nada dele muda.

    Destinatário continua vindo da lista de telefones do `.env`, **sem vínculo
    com pessoa**: não há telefone em `usuarios`, e por isso o WhatsApp não
    resolve destinatário por papel como o e-mail resolve. A assimetria é
    consequência do dado que existe, não escolha de desenho (ADR 0006), e some
    no dia em que `Usuario` tiver telefone — o que é outra decisão, com o seu
    próprio custo de LGPD.
    """

    canal = Canal.WHATSAPP

    def __init__(self, *, habilitado: bool, provider: WhatsAppProvider | None) -> None:
        self.habilitado = habilitado
        self._provider = provider
        self._memoria: dict[TipoAlerta, list[Destinatario]] | None = None

    def disponivel(self) -> bool:
        return self._provider is not None

    def destinatarios(
        self, session: Session, settings: Settings, tipo: TipoAlerta
    ) -> list[Destinatario]:
        del session  # o telefone vem da configuração, não do banco.
        if self._memoria is None:
            self._memoria = {
                tipo_configurado: [Destinatario(endereco=numero) for numero in numeros]
                for tipo_configurado, numeros in config.destinatarios(settings).items()
            }
        return self._memoria.get(tipo, [])

    def renderizar(
        self, tipo: TipoAlerta, contexto: dict[str, str], settings: Settings
    ) -> MensagemAlerta:
        return templates.renderizar(Canal.WHATSAPP, tipo, contexto, settings)

    def enviar(self, endereco: str, mensagem: MensagemAlerta) -> None:
        if self._provider is None:  # pragma: no cover - o serviço filtra antes
            raise EnvioError("canal de WhatsApp sem gateway configurado")
        self._provider.enviar(endereco, mensagem.corpo)


class CanalEmail:
    """Embrulha o `EmailProvider` do `mailer`, que **não é alterado**.

    Destinatário é resolvido por **papel**, e isso fecha uma limitação que a
    issue #30 registrou: telefone solto no `.env` não tem vínculo com pessoa
    nenhuma, e quem sai da equipe continua recebendo até alguém lembrar de
    editar a variável. Aqui, desativar a conta já tira a pessoa da lista.
    """

    canal = Canal.EMAIL

    def __init__(self, *, habilitado: bool, provider: EmailProvider | None) -> None:
        self.habilitado = habilitado
        self._provider = provider
        self._memoria: dict[TipoAlerta, list[Destinatario]] = {}

    def disponivel(self) -> bool:
        return self._provider is not None

    def destinatarios(
        self, session: Session, settings: Settings, tipo: TipoAlerta
    ) -> list[Destinatario]:
        """Contas ativas dos papéis configurados para este tipo.

        Memorizado por tipo porque a varredura roda de minuto em minuto pelo
        cron e pode carregar dezenas de alertas do mesmo tipo numa passada:
        sem isto, resolver destinatário por papel viraria uma consulta a
        `usuarios` por alerta. A memória vale por uma varredura — a instância
        do canal é construída por `construir_canais` a cada uma.
        """
        if tipo not in self._memoria:
            papeis = config.papeis_por_tipo(settings).get(tipo, ())
            self._memoria[tipo] = repository.usuarios_ativos_por_papel(session, papeis=papeis)
        return self._memoria[tipo]

    def renderizar(
        self, tipo: TipoAlerta, contexto: dict[str, str], settings: Settings
    ) -> MensagemAlerta:
        return templates.renderizar(Canal.EMAIL, tipo, contexto, settings)

    def enviar(self, endereco: str, mensagem: MensagemAlerta) -> None:
        if self._provider is None:  # pragma: no cover - o serviço filtra antes
            raise EnvioError("canal de e-mail sem SMTP configurado")
        assunto = mensagem.assunto if mensagem.assunto is not None else ""
        try:
            self._provider.enviar(endereco, assunto, mensagem.corpo)
        except EnvioEmailError as exc:
            # Traduz para a família de erro que o serviço sabe registrar como
            # linha `falha`. A mensagem já vem sem a senha SMTP (mailer/smtp.py)
            # e vai para `alertas_enviados.detalhe`.
            raise EnvioError(str(exc)) from exc


def construir_canais(settings: Settings) -> list[CanalAlerta]:
    """Todos os canais implementados, cada um sabendo se está ligado e se pode enviar.

    Devolve **todos**, e não só os ligados, porque o resumo da varredura precisa
    responder pelos dois estados de cada canal (`ResumoVarredura.canais`): um
    canal que sumisse da lista seria indistinguível de um canal que ninguém
    olhou, e é justamente a diferença entre "desliguei" e "esqueci de
    configurar" que o ADR 0006 manda mostrar separada.

    Construir é barato e não faz E/S: as duas factories decidem só a partir da
    configuração e devolvem `None` sem credencial.

    **É aqui que a parte 2 do ADR entra**: `canais_habilitados` passa a ler a
    tabela de canais em vez de `ALERTAS_CANAIS`. Nada mais desta trilha muda.
    """
    habilitados = config.canais_habilitados(settings)
    return [
        CanalWhatsApp(
            habilitado=Canal.WHATSAPP in habilitados,
            provider=get_provider(settings),
        ),
        CanalEmail(
            habilitado=Canal.EMAIL in habilitados,
            provider=get_email_provider(settings),
        ),
    ]


def canais_que_enviam(canais: list[CanalAlerta]) -> list[CanalAlerta]:
    """Os canais habilitados **e** disponíveis — os únicos que despacham qualquer coisa."""
    return [canal for canal in canais if canal.habilitado and canal.disponivel()]
