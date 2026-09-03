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

## Relatórios e métricas

Dois produtos sobre o mesmo dado, com públicos diferentes (issue #8), sob
`/api/relatorios` — protegido por `X-API-Key` como todo o resto de `/api/*`:

- `GET /conferencia` e `GET /conferencia.csv` — o **relatório operacional**: uma
  linha por documento, com o problema encontrado e a ação necessária já
  resolvidos no backend. Filtra por competência, status, operadora, paciente,
  janela de recebimento e `apenas_pendentes`. Ordena pelo que precisa de ação
  humana primeiro (incompleto, problema, em correção), depois pelo prazo mais
  próximo — não pelo ciclo de vida do documento.
- `GET /metricas` — as **métricas agregadas** por competência, operadora e dia.
- `GET`/`PUT /baseline` — o cadastro de glosa informada à mão, que é o que
  torna a comparação antes/depois possível.

### A honestidade da comparação antes/depois

O sistema mede **pendência detectada antes do envio**; o baseline registra
**glosa**, o que a operadora recusou depois do envio. São medidas diferentes e
**não são divididas uma pela outra**: a resposta de `/metricas` expõe os dois
blocos lado a lado e nomeados (`sistema` e `glosa_informada`), nunca fundidos
num único número de "ROI". A comparação que o backend calcula
(`comparacao_glosa`) é glosa contra glosa — a competência mais antiga com
baseline contra a mais recente com baseline, mesma medida nas duas pontas — e é
`None` enquanto não houver duas.

`documentos_com_pendencia` (documento com ao menos uma pendência, em qualquer
status) é a medida estável de "quantos exigiram intervenção" e é ela que serve
para acompanhar mês a mês; `por_status` é foto do status **atual** e por isso
melhora justamente quando a correção funciona. As definições completas estão na
docstring de `src/homecareos/reports/metricas.py`.

### CSV, e não `.xlsx`

`GET /conferencia.csv` sai com delimitador `;` e BOM UTF-8: é o que faz o
arquivo abrir com colunas separadas e acentuação correta no Excel em português.
Um `.xlsx` de verdade exigiria dependência nova (`openpyxl`) sem ganho neste
momento — **desvio consciente** da issue, que pede "CSV/Excel", registrado
também na docstring de `src/homecareos/reports/csv_export.py`.

## Alertas de WhatsApp

Quatro alertas (issue #9), enviados por um gateway de WhatsApp e registrados em
`alertas_enviados`. Endpoints sob `/api/alertas` — protegidos por `X-API-Key`
como todo o resto de `/api/*`:

- `POST /varredura` — roda os quatro detectores e envia o que for novo. É o
  mesmo trabalho de `python -m homecareos.alerts.scan`
  (`docker compose run --rm api-alertas`), que é o que o cron chama.
- `GET ""` — o log paginado do que foi enviado, falhou ou foi suprimido,
  filtrável por `tipo`, `status` e `documento_id`.

### uazapi, e não Z-API — desvio consciente

A issue #9 nomeia a Z-API; o gateway contratado é a **uazapi**, e é ela que
está implementada. O desvio é barato porque a camada de alertas só conhece a
porta `WhatsAppProvider` ("entregue este texto para este número, ou levante
`EnvioError`"): trocar de gateway é escrever outra implementação e mudar
configuração, não reescrever detector, template ou log. O contrato verificado
da uazapi (header `token` literal e minúsculo, `POST /send/text`) está na
docstring de `src/homecareos/alerts/uazapi.py`.

O token da instância é credencial de envio e **nunca** aparece em log, `repr`,
mensagem de exceção ou linha do banco. `alertas_enviados.mensagem`, por outro
lado, guarda o texto enviado — inclusive o nome do paciente — e isso é decisão
consciente de auditabilidade, justificada em `src/homecareos/db/models/alerta.py`.

### Os quatro detectores

| Tipo | Dispara quando |
| --- | --- |
| `documento_incompleto_critico` | documento `incompleto` com pendência não resolvida em `carimbo_presente`, `carimbo_legivel` ou `assinatura_profissional_presente` |
| `deadline_competencia` | há pendência em aberto com deadline dentro de `ALERTAS_DIAS_ANTES_DEADLINE`, agrupado por competência e operadora |
| `volume_anormal` | a taxa de problema de hoje passa da média da janela vezes `ALERTAS_VOLUME_FATOR` |
| `pendencia_parada` | pendência `aberta` (não `em_correcao`) há mais de `ALERTAS_HORAS_PENDENCIA_PARADA` |

`volume_anormal` só dispara com pelo menos `ALERTAS_VOLUME_MINIMO_DOCUMENTOS`
documentos no dia e janela de referência não vazia. Sem esse piso, um único
documento com problema num dia parado dá 100% de taxa e dispara alerta todo dia
— o jeito mais rápido de ensinar a equipe a ignorar o WhatsApp.

### Anti-bombardeio: duas defesas, uma delas silenciosa

- **Cooldown** (`ALERTAS_COOLDOWN_HORAS`, mesmo assunto para o mesmo número):
  pula **sem gravar linha**. A varredura roda de minuto em minuto; registrar
  cada supressão encheria a tabela com centenas de linhas por dia por alerta e
  esconderia as falhas de verdade no meio do ruído.
- **Rate limit** (`ALERTAS_MAX_POR_HORA_POR_DESTINATARIO`): **grava** linha
  `suprimido` com o motivo. Essa supressão é anômala — alguma notificação real
  se perdeu — e alguém precisa poder descobrir isso depois.

### O gancho na classificação

Com `ALERTAS_HOOK_INLINE_HABILITADO=true` (padrão), o alerta de documento
incompleto crítico sai já na classificação, sem esperar a varredura. O gancho é
síncrono e está no caminho do upload (teto: `ALERTAS_TIMEOUT_SEGUNDOS`), abre a
própria sessão e **nunca levanta**: notificação não pode derrubar ingestão de
documento. `false` desliga o gancho e deixa o caso para a varredura periódica.

## Autenticação

Duas credenciais convivem, e não se substituem (ADR 0001, issue #30):

| credencial | quem usa | como |
| --- | --- | --- |
| **Sessão de usuário** | pessoas, pelo navegador | `POST /api/auth/login` devolve um cookie `httpOnly`; a sessão vive na tabela `sessoes` |
| **`X-API-Key`** | integração máquina-a-máquina | header, como antes — é o que o cron `python -m homecareos.alerts.scan` usa |

A sessão tem estado no Postgres (e não é um JWT) por causa da revogação:
desligar o acesso de alguém a prontuário clínico não pode esperar um token
expirar. Desativar o usuário (`usuarios.ativo = false`) derruba o acesso na
requisição seguinte.

A senha é hasheada com **Argon2id** e nunca é gravada, logada nem devolvida em
claro. O cookie carrega um token opaco de 256 bits; o banco guarda só o SHA-256
dele — um dump vazado não entrega sessão utilizável.

`POST /api/auth/login` é a única rota de `/api/*` que nasce sem exigir
credencial (não dá para exigir sessão para criar sessão). Login com e-mail
inexistente, senha errada e usuário inativo respondem **exatamente igual**, e o
caminho do e-mail inexistente ainda gasta uma verificação Argon2 descartável —
sem isso, o tempo de resposta diria quem está cadastrado.

### Matriz de papéis

| rota | conferente | coordenador | gestor |
| --- | :-: | :-: | :-: |
| `POST /api/documentos` | ✅ | ✅ | — |
| `GET /api/documentos`, `GET /api/documentos/{id}` | ✅ | ✅ | ✅ |
| `POST /api/documentos/{id}/revalidar` | ✅ | ✅ | — |
| `GET /api/pendencias`, `/resumo` | ✅ | ✅ | ✅ |
| `PATCH /api/pendencias/{id}` | ✅ | ✅ | — |
| `GET /api/operadoras`, `GET /api/pacientes` | ✅ | ✅ | ✅ |
| `/api/regras` (todos os métodos) | — | ✅ | — |
| `GET /api/relatorios/conferencia`, `.csv` | ✅ | ✅ | ✅ |
| `GET /api/relatorios/metricas`, `GET /api/relatorios/baseline` | — | ✅ | ✅ |
| `PUT /api/relatorios/baseline` | — | — | ✅ |
| `POST /api/alertas/varredura`, `GET /api/alertas` | — | ✅ | ✅ |

`conferente` está contido em `coordenador`. `gestor` **não** é superconjunto de
ninguém: é outro eixo — lê a operação inteira, não a executa, e é o único que
escreve baseline, que é dado de gestão e não de conferência.

**A autorização por papel só se aplica a sessão de usuário.** Requisição
autenticada por `X-API-Key` passa por qualquer checagem de papel: a chave sempre
deu acesso total a `/api/*` e é dela que dependem as integrações existentes.
Estreitá-la é outra decisão, com outro ADR — não um ajuste desta trilha.

`POST /api/pacientes` não consta da matriz aprovada e por isso herda a regra do
router (os três papéis), que é o comportamento que já existia.

### Criar o primeiro usuário

```bash
cd apps/api
uv run python -m homecareos.auth.cli criar \
  --nome "Ana Souza" --email ana@exemplo.com --papel coordenador
# Senha: (lida por prompt, sem eco)
```

A senha **nunca** vem em argumento de linha de comando: ali ela ficaria no
histórico do shell e apareceria em `ps` para qualquer outro usuário da máquina.
Papel inválido e e-mail duplicado saem com código 1 e mensagem clara.

### Limitações conhecidas

Esta entrega **não** tem, e é deliberado — cada um destes itens é decisão de
produto/segurança que merece a sua própria issue, e uma versão frouxa seria pior
que a ausência:

- **sem bloqueio por tentativa de login e sem rate limit**: força bruta contra a
  API não é freada pela aplicação hoje;
- **sem recuperação de senha**: quem esquece a senha depende de alguém rodar o
  CLI de novo;
- **sem MFA**;
- **sem CRUD de usuário via API**: criar, editar, desativar e listar usuário é
  operação de banco ou CLI. A matriz aprovada não diz quem administra usuário, e
  decidir isso sem o cliente seria inventar requisito — um `POST /api/usuarios`
  aberto ao papel errado deixaria qualquer um criar um `gestor` e escalar
  sozinho.
