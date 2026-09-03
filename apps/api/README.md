# homecareos-api

API do HomeCareOS (FastAPI + SQLAlchemy + Alembic). Ver o README na raiz do
repositório para setup local completo via Docker Compose.

## Catálogo de regras

`python -m homecareos.seed` (via `docker compose run --rm api-seed`) popula,
além das operadoras, a biblioteca inicial de regras de validação — issue #10.
São duas famílias, separadas pela coluna `regras.escopo`:

- **`tiss`** — regras genéricas, fundamentadas em fonte pública verificável
  (RDC Anvisa 11/2006, Resolução Cofen 754/2024, padrão TISS/ANS). Nascem
  **ativas** e são materializadas para todas as operadoras cadastradas (uma
  linha por operadora, já que `regras.operadora_id` é `NOT NULL`).
- **`operadora`** — regras candidatas específicas de Amil e Unimed. Nascem
  **inativas** (`ativo = false`), porque o manual do prestador dessas
  operadoras não é público; a `fonte` começa por `A CONFIRMAR —` e a operação
  ativa a regra quando confirmar a exigência no manual vigente.

Os JSON do catálogo vivem em `src/homecareos/rules/data/` (`tiss_generico.json`,
`amil.json`, `unimed.json`) e são carregados por `rules/catalogo.py`, que
valida cada regra contra a gramática de `Condicao` e contra os campos de
`EvolucaoProntuario` antes de deixá-la chegar ao banco.

O seed (`rules/seed_regras.py`) é idempotente via
`INSERT ... ON CONFLICT (operadora_id, codigo) DO NOTHING` e **nunca reativa**
uma regra que a operação desativou: rodar o seed de novo em todo deploy não
desfaz ajuste nenhum feito à mão no banco. Mudar o conteúdo de uma regra já
cadastrada é migration de dados explícita, não efeito colateral de seed.
