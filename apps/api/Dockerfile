# Imagem da API do HomeCareOS.
#
# Duas etapas: a primeira instala as dependências de runtime com `uv` num
# virtualenv; a segunda leva só o virtualenv e o código-fonte. O `uv`, o cache
# de resolução e as dependências de dev (pytest, ruff, mypy) ficam na primeira
# etapa e não chegam à imagem final.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev --no-editable

COPY src ./src
COPY alembic.ini ./
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/alembic.ini ./
COPY --from=builder /app/src ./src

# Usuário sem privilégio. A porta é 8000, acima de 1024, então nada aqui
# precisa de root.
RUN useradd --system --uid 10001 --shell /usr/sbin/nologin homecareos \
    && chown -R homecareos:homecareos /app
USER homecareos

EXPOSE 8000
CMD ["uvicorn", "homecareos.main:app", "--host", "0.0.0.0", "--port", "8000"]
