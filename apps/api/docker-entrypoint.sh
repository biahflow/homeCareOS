#!/bin/sh
# O Dockerfile deste serviço instala as dependências dentro da imagem, mas o
# código-fonte chega por volume montado pelo Compose (ver o comentário sobre
# PYTHONPATH em docker-compose.yml). Isso separa duas fontes de verdade: o
# código pode mudar sem rebuild, mas as dependências não — elas só existem na
# imagem. Quando pyproject.toml/uv.lock muda e ninguém roda
# `docker compose build`, o container sobe com código novo e dependências
# velhas, e o erro só aparece depois, como um ModuleNotFoundError sem relação
# aparente com a causa real. Este script compara o uv.lock congelado na
# imagem no build com o uv.lock do projeto (montado pelo Compose) antes de
# deixar o processo de verdade começar, e recusa arrancar quando os dois
# divergem — com uma mensagem que diz o comando certo, em vez de deixar o
# sintoma aparecer escondido lá na frente.
#
# Falha ABERTA quando falta informação (um dos locks ausente — produção sem
# volume, ou imagem antiga sem .image-uv.lock) e só falha FECHADA com
# evidência positiva de divergência entre os dois arquivos: uma checagem de
# arranque que pudesse derrubar a API por um falso positivo seria pior do que
# o defeito que ela existe para resolver.
set -eu

IMAGE_LOCK="${HOMECAREOS_IMAGE_LOCK:-/app/.image-uv.lock}"
MOUNTED_LOCK="${HOMECAREOS_MOUNTED_LOCK:-/app/uv.lock}"

if [ "${HOMECAREOS_SKIP_LOCK_CHECK:-}" = "1" ]; then
    echo "AVISO: verificação de uv.lock pulada (HOMECAREOS_SKIP_LOCK_CHECK=1)." >&2
elif [ -f "$IMAGE_LOCK" ] && [ -f "$MOUNTED_LOCK" ]; then
    if ! cmp -s "$IMAGE_LOCK" "$MOUNTED_LOCK"; then
        # Delimitador entre aspas: desliga expansão de variável e de
        # crase no heredoc. Sem isso o shell tenta EXECUTAR o que estiver
        # entre crases no texto da mensagem, e o que sobra chega ao
        # usuário mutilado — logo a mensagem que existe para ser clara.
        cat >&2 <<'EOF'
ERRO: a imagem está desatualizada em relação ao uv.lock do projeto.

As dependências vêm da imagem; o código vem do volume. Alguma alteração em
pyproject.toml/uv.lock ainda não foi instalada na imagem, então o import vai
falhar em runtime.

    docker compose --profile tools build && docker compose up -d

O `--profile tools` não é enfeite: api, api-migrate, api-seed e api-alertas
compartilham este Dockerfile mas constroem imagens SEPARADAS, e um
`docker compose build` sem ele alcança só a do api.

Para pular esta verificação: HOMECAREOS_SKIP_LOCK_CHECK=1
EOF
        exit 1
    fi
fi
# Um dos dois locks ausente (ou os dois) não é divergência comprovada — só
# falta de informação — e por isso não impede o arranque.

exec "$@"
