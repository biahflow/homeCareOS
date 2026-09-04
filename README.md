# HomeCareOS

Conferência pré-faturamento de evoluções de prontuário: evoluções (PDF/imagem
escaneada, com data, carimbo COREN e assinatura) são recebidas, validadas
contra regras da operadora, e pendências são sinalizadas antes do envio à
operadora — reduzindo glosas.

Stack: Python 3.12 + uv · FastAPI · SQLAlchemy 2 + Alembic · Postgres 17 ·
MinIO/S3 · Next.js (frontend, trilha separada).

## Setup local (< 5 minutos)

Pré-requisitos: Docker e Docker Compose.

```bash
cp .env.example .env
docker compose up -d
curl -f localhost:8001/health
# {"status":"ok"}
# front em http://localhost:3001
```

O `up` inclui o build do frontend, que na primeira vez leva menos de um minuto
(medido: 43s). Para trabalhar só no backend, `docker compose up -d api` sobe a
API com Postgres e MinIO e pula essa espera.

Se `pyproject.toml`/`uv.lock` mudar (dependência nova), é preciso rebuild antes
do próximo `up`/`run`:

```bash
docker compose --profile tools build && docker compose up -d
```

O `--profile tools` importa: `api`, `api-migrate`, `api-seed`, `api-alertas` e
`api-retencao` compartilham o mesmo Dockerfile mas constroem imagens
**separadas**, e um `build` sem ele alcança só a do `api`. O comando acima
reconstrói também o `web`, que não está em profile nenhum.

Esquecer o rebuild não passa em silêncio: o container recusa arrancar e
`docker compose logs api` diz o que fazer, em vez de morrer num
`ModuleNotFoundError` sem contexto. A verificação acontece no arranque, então
um container que já estava no ar quando o `uv.lock` mudou segue rodando —
`docker compose restart api` força a checagem.

Migrations e seed são ferramentas sob demanda (não sobem com `up`, porque
dependem de código de outras trilhas):

```bash
docker compose run --rm api-migrate
docker compose run --rm api-seed
```

`api-seed` popula as operadoras conhecidas **e** o catálogo de regras (ver
"Catálogo de regras" em `apps/api/README.md`).

Para derrubar tudo, incluindo os volumes de dados:

```bash
docker compose down -v
```

## Desenvolvimento da API

Pré-requisito: [uv](https://docs.astral.sh/uv/).

```bash
cd apps/api
uv sync
```

### Portões de qualidade

```bash
cd apps/api
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

Os testes marcados como `integration` falam com o Postgres de verdade: rode
`alembic upgrade head` e `python -m homecareos.seed` (ou os serviços
`api-migrate`/`api-seed` do Compose) contra o banco apontado por
`DATABASE_URL` antes do `pytest`, senão as tabelas não existem.

O mesmo conjunto roda no CI (`.github/workflows/quality.yml`), com Postgres 17
como service container, migration e seed aplicados antes do `pytest`.

## Desenvolvimento do frontend

Pré-requisito: Node >= 22.13. O `npm ci` é sempre na **raiz** — o lockfile é do
workspace inteiro.

```bash
npm ci
npm run dev -w apps/web
# http://localhost:3000
```

Rodando pelo Compose (`docker compose up -d web`) o front é publicado em
`WEB_PORT` (default **3001**), porque a 3000 fica reservada ao `next dev`.

### Portões de qualidade

```bash
npm run lint -w apps/web
npx tsc --noEmit -p apps/web/tsconfig.json
npx tsc --noEmit -p packages/contracts/tsconfig.json
npm run build -w apps/web
```

Rodam no CI como o job `web`, independente do job `api`.

### Como o front fala com a API

O navegador **não** conhece a URL da API: ele chama `/api/*` na própria origem
do Next, e o servidor do Next repassa para a API em `apps/web/proxy.ts` (o que
até o Next 15 se chamava `middleware.ts`). É o que mantém o cookie de sessão
como same-site e dispensa CORS na API — ver
[ADR 0002](docs/adr/0002-proxy-do-frontend-para-a-api.md).

O repasse **não** usa `rewrites` do `next.config.ts` de propósito: o
`next build` congela a URL de destino no manifesto de rotas, e a imagem sairia
apontando para o ambiente onde foi construída.

A URL de destino é a variável de servidor `API_URL` (default
`http://localhost:8001`); no Compose, o serviço `web` usa `http://api:8000`.

## Estrutura

```text
apps/
├─ api/     # FastAPI + SQLAlchemy + Alembic (Python 3.12, uv)
└─ web/     # Next.js 16 (App Router, TypeScript, Tailwind)
packages/
└─ contracts/  # Tipos do contrato da API + cliente HTTP, sem React
docs/
├─ adr/       # Decisões de arquitetura com impacto durável
└─ features/  # Feature Contracts e artefatos de planejamento
```

`apps/web` e `packages/*` formam um workspace npm, com `package-lock.json`
único na raiz.

Um ADR (`docs/adr/NNNN-titulo.md`) registra escolha com impacto durável em
arquitetura, operação, segurança ou custo — contexto, decisão, consequências e
alternativas consideradas, incluindo o que foi descartado e por quê.
