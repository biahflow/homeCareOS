"""Fumaça de configuração SMTP: `python -m homecareos.mailer.cli --para <email>`.

Não existia forma de verificar se a credencial de SMTP funciona sem disparar
um fluxo real (recuperação de senha, ou a varredura de alertas) — quem
configura credencial nova só descobria o erro quando um alerta crítico não
chegava. Este comando monta o provider pelo mesmo caminho que a aplicação usa
(`mailer.provider.get_email_provider(get_settings())`) e envia um e-mail de
teste de verdade, ou explica exatamente por que não enviou.

## Saída: legível para humano, não JSON

Ao contrário de `alerts/scan.py` e `retencao/cli.py` — que imprimem JSON porque
quem lê é um cron —, este comando é ferramenta de **operador**: quem configura
SMTP roda uma vez, olha a tela e decide se está certo. O par stdout/stderr +
código de saída segue o mesmo precedente (sucesso legível em stdout, problema
em stderr, código diferente de zero só quando alguém precisa agir), só que o
conteúdo de stdout é texto, não `model_dump_json()`.

## A pegadinha do `.env`

`Settings` lê o `.env` do **diretório de trabalho** (ver `config.py`), e o
`.env` deste projeto fica na raiz do repositório, não em `apps/api`. Rodando
`python -m homecareos.mailer.cli` de dentro de `apps/api`, o `.env` da raiz
não é lido e o provider vira `None` **silenciosamente** — o comando diria "SMTP
não configurado" numa máquina onde ele está perfeitamente configurado. A
mesma pegadinha já morde o alembic (ver a nota em `apps/api/README.md`, seção
de migrations).

Três formas de evitar isso, da mais para a menos recomendada:

1. `docker compose run --rm api-email-teste --para ...` — o `env_file` do
   Compose aponta para o `.env` da raiz, então o problema não existe.
2. Rodar `python -m homecareos.mailer.cli` a partir da **raiz** do repositório.
3. Exportar as variáveis `SMTP_*` no ambiente antes de rodar de `apps/api`.

Quando o provider vem `None` e a heurística abaixo reconhece o padrão exato
desta pegadinha (nenhum `.env` no diretório atual, mas um `.env` **e** um
`docker-compose.yml` juntos num diretório ancestral — a marca da raiz deste
repositório), o aviso sai também no stderr. A heurística é deliberadamente
restrita: ela não tenta adivinhar "você esqueceu de configurar" quando o
motivo é outro, porque um palpite errado atrapalha mais do que ajuda.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from homecareos.config import Settings, get_settings
from homecareos.mailer.errors import EnvioEmailError
from homecareos.mailer.provider import get_email_provider

ASSUNTO_PADRAO = "HomeCareOS — teste de configuração de e-mail"
CORPO_PADRAO = (
    "Este é um e-mail de teste enviado por `python -m homecareos.mailer.cli` "
    "para verificar a configuração de SMTP do HomeCareOS. Se você recebeu "
    "esta mensagem, o envio está funcionando.\n"
)


def _campos_faltantes(settings: Settings) -> list[str]:
    """Os campos que fazem `get_email_provider` devolver `None` — ver a
    docstring dele: `smtp_host` OU `smtp_remetente` vazio já basta."""
    faltantes = []
    if not settings.smtp_host:
        faltantes.append("SMTP_HOST")
    if not settings.smtp_remetente:
        faltantes.append("SMTP_REMETENTE")
    return faltantes


def _mensagem_smtp_nao_configurado(settings: Settings) -> str:
    faltantes = _campos_faltantes(settings)
    verbo = "falta" if len(faltantes) == 1 else "faltam"
    return f"SMTP não configurado: {verbo} {' e '.join(faltantes)}."


def _dica_env_ausente() -> str | None:
    """`None` quando a heurística não reconhece com segurança o padrão da
    pegadinha do `.env` — ver a docstring do módulo. Só avisa quando: (a) não
    há `.env` no diretório de trabalho atual, e (b) existe um diretório
    ancestral com `.env` **e** `docker-compose.yml` juntos, a marca da raiz
    deste repositório. As duas condições evitam o falso positivo de avisar
    quando o motivo real é só um campo vazio no `.env` certo.
    """
    cwd = Path.cwd()
    if (cwd / ".env").is_file():
        return None
    for ancestral in cwd.parents:
        if (ancestral / ".env").is_file() and (ancestral / "docker-compose.yml").is_file():
            return (
                f"não há .env em {cwd}, e Settings lê o .env do diretório de "
                f"trabalho — há um em {ancestral} (raiz do repositório). Rode a "
                "partir de lá, exporte as variáveis SMTP_* manualmente, ou use "
                "'docker compose run --rm api-email-teste --para ...'."
            )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m homecareos.mailer.cli",
        description=(
            "Fumaça de configuração SMTP: monta o provider de e-mail a partir "
            "da configuração real (o mesmo caminho da recuperação de senha e "
            "dos alertas por e-mail) e envia uma mensagem de teste. Prefira "
            "rodar via 'docker compose run --rm api-email-teste --para ...' — "
            "é lá que a configuração precisa provar que funciona, e o env_file "
            "do Compose evita a pegadinha abaixo."
        ),
        epilog=(
            "ATENÇÃO: Settings lê o .env do diretório de trabalho. O .env deste "
            "projeto fica na RAIZ do repositório, não em apps/api. Rodando este "
            "comando de dentro de apps/api sem exportar as variáveis SMTP_*, o "
            "provider vira None silenciosamente mesmo com SMTP configurado — "
            "rode da raiz do repositório, exporte as variáveis, ou use o "
            "serviço 'api-email-teste' do Compose."
        ),
    )
    parser.add_argument("--para", required=True, help="Destinatário do e-mail de teste.")
    parser.add_argument("--assunto", default=ASSUNTO_PADRAO, help="Assunto do e-mail de teste.")
    parser.add_argument("--corpo", default=CORPO_PADRAO, help="Corpo do e-mail de teste.")
    args = parser.parse_args(argv)

    settings = get_settings()
    provider = get_email_provider(settings)
    if provider is None:
        print(_mensagem_smtp_nao_configurado(settings), file=sys.stderr)
        dica = _dica_env_ausente()
        if dica is not None:
            print(dica, file=sys.stderr)
        return 1

    try:
        provider.enviar(args.para, args.assunto, args.corpo)
    except EnvioEmailError as exc:
        # A mensagem já vem sem a senha (`SmtpEmailProvider._sem_a_senha`) —
        # ver a docstring de `mailer/smtp.py`. Não reimplementar o mascaramento
        # aqui, só propagar o texto que o provider já produziu.
        print(f"falha ao enviar e-mail de teste: {exc}", file=sys.stderr)
        return 1

    autenticado = "sim" if settings.smtp_usuario else "não"
    print(f"host: {settings.smtp_host}")
    print(f"porta: {settings.smtp_porta}")
    print(f"remetente: {settings.smtp_remetente}")
    print(f"destinatário: {args.para}")
    print(f"autenticado com usuário: {autenticado}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
