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

Migrations e seed são ferramentas sob demanda (não sobem com `up`, porque
dependem de código de outras trilhas):

```bash
docker compose run --rm api-migrate
docker compose run --rm api-seed
```

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

O mesmo conjunto roda no CI (`.github/workflows/quality.yml`), com Postgres 17
como service container.

## Estrutura

```text
apps/
├─ api/     # FastAPI + SQLAlchemy + Alembic (Python 3.12, uv)
└─ web/     # Next.js (trilha separada)
docs/
└─ features/  # Feature Contracts e artefatos de planejamento
```
