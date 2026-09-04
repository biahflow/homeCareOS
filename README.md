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
```

Se `pyproject.toml`/`uv.lock` mudar (dependência nova), é preciso rebuild antes
do próximo `up`/`run`:

```bash
docker compose --profile tools build && docker compose up -d
```

O `--profile tools` importa: `api`, `api-migrate`, `api-seed` e `api-alertas`
compartilham o mesmo Dockerfile mas constroem imagens **separadas**, e um
`build` sem ele alcança só a do `api`.

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

## Estrutura

```text
apps/
├─ api/     # FastAPI + SQLAlchemy + Alembic (Python 3.12, uv)
└─ web/     # Next.js (trilha separada)
docs/
├─ adr/       # Decisões de arquitetura com impacto durável
└─ features/  # Feature Contracts e artefatos de planejamento
```

Um ADR (`docs/adr/NNNN-titulo.md`) registra escolha com impacto durável em
arquitetura, operação, segurança ou custo — contexto, decisão, consequências e
alternativas consideradas, incluindo o que foi descartado e por quê.
