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

## O documento escaneado

`GET /api/documentos/{id}/arquivo` serve a página escaneada (issue #51). A
conferência é comparar o que a extração leu com o que está no papel, e até esta
entrega a interface não tinha como mostrar o papel: `documentos.arquivo_url`
guarda a **chave** do objeto no storage (`documentos/{uuid}/{sha256}.png`), não
uma URL.

**Streaming pela API, e não URL assinada** — ADR 0003. Em resumo: o presigned do
MinIO é assinado sobre `S3_ENDPOINT_URL`, que no Compose é `http://minio:9000`
(rede interna, que o navegador não alcança, e o host entra na assinatura); o
`LocalDocumentStorage` devolve `file://`; e streaming mantém o prontuário atrás
da mesma autorização do resto da API, em vez de um link que vive fora da sessão
até expirar.

O arquivo sai em blocos de 64 KiB, com `Content-Type` deduzido da extensão da
chave e `Content-Disposition: inline` (quem confere quer ver, não baixar).
**Documento inexistente e documento cujo arquivo não está no storage respondem
o mesmo 404**: arquivo que sumiu do bucket não é defeito da aplicação. Storage
fora do ar continua sendo 503.

O campo `arquivo_url` **mantém o nome apesar de ser uma chave**, e é decisão
consciente: renomeá-lo é quebra de contrato e precisa mudar API, `packages/contracts`
e `apps/web` juntos. Ele ganhou descrição explícita no OpenAPI, e o endpoint
acima remove o motivo de alguém tentar usá-lo como endereço.

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
| `GET /api/documentos/{id}/arquivo` | ✅ | ✅ | ✅ |
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

### Bloqueio por tentativa de login

`POST /api/auth/login` tem freio contra força bruta (issue #33), defesa em
profundidade com três mecanismos independentes (ver `auth/protecao.py`):

| mecanismo | quando age | parâmetro |
| --- | --- | --- |
| **Atraso progressivo** | a cada falha da combinação conta+IP | `LOGIN_ATRASO_BASE_SEGUNDOS` (0.25s), dobra por falha até `LOGIN_ATRASO_MAXIMO_SEGUNDOS` (2s) |
| **Trava de IP** | falhas daquele IP na janela `>= LOGIN_FALHAS_PARA_TRAVAR_IP` (10) **e nenhum login bem-sucedido do mesmo IP na janela** | IP compartilhado é o caso comum, não a exceção: atrás de proxy a empresa inteira chega com um IP só, e contar falhas cruas trancaria todo mundo no começo do turno. Rede com gente trabalhando tem login que funciona; quem sonda senha não tem nenhum |
| **Trava de conta** | falhas daquele e-mail na janela `>= LOGIN_FALHAS_PARA_TRAVAR_CONTA` (20), zerada pelo último sucesso daquele e-mail | último recurso, de propósito: um limiar baixo permitiria que qualquer um que soubesse o e-mail de alguém mantivesse essa pessoa fora do sistema |

`LOGIN_JANELA_MINUTOS` (15) é a janela de observação, e `LOGIN_TRAVA_MINUTOS`
(15) é o tempo reportado no header `Retry-After` do 429. A contagem é sempre
pela **string de e-mail tentada**, exista ou não a conta — contar só para
e-mail cadastrado reabriria a enumeração de usuário que a issue #30 fechou. A
resposta de bloqueio é genérica e idêntica para trava de IP e trava de conta,
pelo mesmo motivo do 401 do login.

Duas notas honestas:

- **`tentativas_login` cresce a cada tentativa de login, sucesso ou falha, e
  não há expurgo automático nesta entrega.** `auth/protecao.limpar_tentativas_antigas`
  existe e apaga linhas antigas, mas não há agendador que a chame — é operação
  manual (ou de um cron futuro) até essa issue existir.
- **`CONFIAR_EM_X_FORWARDED_FOR` precisa ser ligado em deploy atrás de proxy.**
  Sem isso, `request.client.host` é o IP do balanceador para toda requisição,
  e a trava de IP trava o mundo inteiro de uma vez na primeira sondagem —
  esta configuração errada não falha aberta, falha **fechada**, e derrubar
  todo mundo é um jeito ruim de descobrir o erro.

### Recuperação de senha por e-mail

Quem esquece a senha se atende sozinho (issue #34), por e-mail:

| rota | corpo | resposta |
| --- | --- | --- |
| `POST /api/auth/senha/esqueci` | `{"email": "..."}` | **204 sempre** |
| `POST /api/auth/senha/redefinir` | `{"token": "...", "nova_senha": "..."}` | 204, ou 422 |

As duas rotas nascem **sem** exigir credencial, como o login: quem esqueceu a
senha não tem sessão para apresentar.

**`/senha/esqueci` responde 204 em todos os caminhos** — e-mail que não existe,
usuário inativo, teto de envios atingido, SMTP não configurado e até falha de
envio. Um 404 para e-mail desconhecido entregaria a lista de quem trabalha na
operação, que é exatamente a enumeração que a issue #30 fechou no login gastando
uma verificação Argon2 descartável só para o tempo de resposta não denunciar
quem existe. Pelo mesmo motivo, SMTP fora do ar não vira 500: falha de envio só
acontece para e-mail que existe, e o status voltaria a distinguir os casos. A
falha é registrada com `logger.exception` no log da aplicação — é lá que o
operador a vê, não na resposta.

Quando o usuário existe e está ativo, o e-mail leva o link
`{FRONTEND_BASE_URL}/redefinir-senha?token=<token>` (é o frontend que renderiza
a tela; a API só valida o token). O token vale `SENHA_RESET_VALIDADE_MINUTOS`
(30), é de **uso único**, e o banco guarda só o SHA-256 dele — um dump vazado
não vira redefinição de senha.

Redefinir com sucesso **revoga todas as sessões abertas do usuário**, inclusive
a de quem está redefinindo. É o ponto da recuperação: se a conta foi
comprometida, trocar a senha sem derrubar as sessões do invasor não resolve
nada. Senha nova, sessões revogadas e token consumido entram num commit só.

Senha que não passa no piso de tamanho (`SENHA_MINIMA_CARACTERES`, 12) responde
422 dizendo o requisito e **não** consome o token — senão digitar uma senha
curta obrigaria a pessoa a pedir outro e-mail. Token inexistente, expirado e já
usado respondem o mesmo 422 genérico.

#### Configuração SMTP

```bash
SMTP_HOST=smtp.exemplo.com
SMTP_PORTA=587
SMTP_USUARIO=sistema@exemplo.com
SMTP_SENHA=...
SMTP_REMETENTE="HomeCareOS <sistema@exemplo.com>"
SMTP_USAR_TLS=true
FRONTEND_BASE_URL=https://app.exemplo.com
```

#### Limitações honestas

- **Sem SMTP configurado, a recuperação fica desligada.** `SMTP_HOST` **ou**
  `SMTP_REMETENTE` vazio faz `get_email_provider` devolver `None`: o endpoint
  continua respondendo 204 (ele responde 204 sempre), registra um `warning` no
  log dizendo que a recuperação está desligada, e o caminho para redefinir
  senha continua sendo o CLI. Não é falha — é modo de operação legítimo em
  ambiente local e homologação —, mas em produção significa que ninguém recebe
  nada e a única pista é o log.
- **Não há proteção por IP neste endpoint.** O teto é por usuário
  (`SENHA_RESET_MAX_POR_HORA`, 3 por hora), o que protege a caixa postal de cada
  pessoa; abuso distribuído contra muitas contas diferentes não é contido aqui.
  É trabalho futuro, junto com o rate limit geral da API.
- **Só STARTTLS (porta 587).** A porta 465, de TLS implícito
  (`smtplib.SMTP_SSL`), não é suportada nesta entrega.
- **O 204 é indistinguível no corpo e no status, não no relógio.** O caminho do
  e-mail cadastrado fala com o servidor SMTP e o do desconhecido não; sob
  medição cuidadosa a diferença de tempo existe. Fechá-la exigiria fila
  assíncrona de envio, que esta entrega não tem.

### Segundo fator (MFA por TOTP)

Quem quiser exigir um segundo fator liga o **TOTP em app autenticador** (Google
Authenticator, Authy, 1Password) — issue #35. TOTP e não código por WhatsApp ou
SMS: funciona offline, não depende do gateway de mensagens estar no ar, não
custa mensagem, é imune a SIM swap, e mantém o fator separado do canal de
recuperação de senha (e-mail) — um e-mail comprometido não entrega os dois.

O segundo fator é **opcional por conta**: quem não ativou continua logando em
uma etapa, com o comportamento de sempre.

#### Ativação (três passos, com a sessão já aberta)

| rota | corpo | resposta |
| --- | --- | --- |
| `POST /api/auth/mfa/iniciar` | — | `{"secret": "...", "otpauth_uri": "otpauth://totp/..."}` |
| `POST /api/auth/mfa/confirmar` | `{"codigo": "123456"}` | `{"codigos": ["a1b2c-3d4e5", ...]}` |
| `POST /api/auth/mfa/desativar` | `{"senha": "...", "codigo": "123456"}` | 204 |

`/mfa/iniciar` grava o segredo **sem ativar nada**: um segredo que o app não
guardou (QR code fechado antes da hora, celular sem bateria) não pode passar a
ser exigido no login seguinte. Chamar de novo antes de confirmar **substitui** o
segredo — quem perdeu o QR code no meio do cadastro recomeça, e um segredo não
confirmado não protege nada. Com MFA já ativado, responde 409.

`/mfa/confirmar` é o que ativa, e só com a prova de que o app guardou o segredo.
Ele devolve os **códigos de recuperação** (`MFA_CODIGOS_RECUPERACAO`, 8) — a
única vez em que eles existem em claro. O banco guarda só o hash Argon2id: não
há endpoint que os mostre de novo.

`/mfa/desativar` exige **senha e código**, os dois. Com só o código, uma sessão
sequestrada desligaria o segundo fator sozinha, que é exatamente o que ele
existe para impedir; com só a senha, bastaria a senha vazada, que é a hipótese
que faz alguém ativar MFA. Senha errada e código errado respondem o mesmo 422.

Os três exigem **sessão de usuário**: requisição por `X-API-Key` responde 403 —
chave de máquina não tem celular nem app autenticador.

#### Login em duas etapas

Com MFA ativado, `POST /api/auth/login` deixa de devolver o usuário:

```text
POST /api/auth/login      -> 200 {"mfa_pendente": true}   + cookie de sessão PENDENTE
POST /api/auth/mfa/verificar  {"codigo": "123456"}  -> 200 {usuário}
```

A sessão do primeiro passo **não abre rota nenhuma** de `/api/*`:
`auth/sessoes.resolver_sessao` devolve `None` para sessão pendente, e é isso que
faz o segundo fator não ser uma tela que dá para pular. Só
`POST /api/auth/mfa/verificar` a enxerga (por `resolver_sessao_pendente`), e por
isso essa rota é pública: a credencial que ela consome é justamente o cookie que
o resto da API recusa.

`/mfa/verificar` aceita o código de seis dígitos **ou** um código de
recuperação, que é de uso único. Falha nos dois responde 401 **e conta em
`tentativas_login`**: são seis dígitos, e o freio da issue #33 ("Bloqueio por
tentativa de login", acima) é o que os protege — sem essa contagem o segundo
fator viraria o alvo mais barato do sistema.

**O mesmo código não funciona duas vezes.** O passo TOTP aceito é gravado em
`usuarios.mfa_ultimo_passo`, e códigos daquele passo ou anteriores passam a ser
recusados. Sem isso, o mesmo código valeria durante toda a janela de tolerância
(`MFA_JANELA_PASSOS`, ±30s) e quem o interceptasse teria ~90 segundos para
reusá-lo.

#### Configuração

```bash
MFA_EMISSOR=HomeCareOS       # nome mostrado no app autenticador e no QR code
MFA_JANELA_PASSOS=1          # tolerância de relógio: ±1 passo (±30s)
MFA_CODIGOS_RECUPERACAO=8    # quantos códigos a ativação gera
```

#### Limitações honestas

- **O segredo TOTP fica em claro no banco** (`usuarios.mfa_secret`). Com um
  dump, o atacante gera códigos válidos — o segundo fator não resiste a quem já
  tem o banco. Não há KMS neste projeto, e "criptografar" com uma chave guardada
  no mesmo `.env` que acompanha o dump seria teatro: quem tem o banco geralmente
  tem a configuração. A limitação é **declarada**, não escondida; fechá-la de
  verdade é KMS/HSM, com a sua própria issue.
- **Não há como reemitir código de recuperação sem desativar o MFA.** Quem
  perder a lista junto com o celular precisa de quem administre o banco: os
  códigos só existem em claro no momento da ativação.
- **Não há política que obrigue MFA.** Ativar é decisão de cada pessoa; não
  existe configuração que o exija por papel ou para a operação inteira. Isso é
  requisito de produto, e inventá-lo aqui trancaria gente para fora sem ninguém
  ter decidido.

### Criar o primeiro usuário

```bash
cd apps/api
uv run python -m homecareos.auth.cli criar \
  --nome "Ana Souza" --email ana@exemplo.com --papel coordenador
# Senha: (lida por prompt, sem eco)
```

A senha **nunca** vem em argumento de linha de comando: ali ela ficaria no
histórico do shell e apareceria em `ps` para qualquer outro usuário da máquina.
Papel inválido e e-mail duplicado saem com código 1 e mensagem clara. O CLI usa
a **mesma** validação de força do endpoint de redefinição
(`SENHA_MINIMA_CARACTERES`): senão o caminho administrativo aceitaria a senha que
o caminho do usuário recusa.

### Limitações conhecidas

Esta entrega **não** tem, e é deliberado — cada um destes itens é decisão de
produto/segurança que merece a sua própria issue, e uma versão frouxa seria pior
que a ausência:

- **sem rate limit geral da API**: o freio da issue #33 cobre só
  `POST /api/auth/login` (ver "Bloqueio por tentativa de login" acima); as
  demais rotas de `/api/*` não têm limite de requisições;
- **recuperação de senha só com SMTP configurado**: ela existe desde a issue #34
  (ver "Recuperação de senha por e-mail" acima), mas sem `SMTP_HOST`/
  `SMTP_REMETENTE` fica desligada e o caminho volta a ser o CLI;
- **sem política que obrigue MFA**: o segundo fator existe desde a issue #35
  (ver "Segundo fator (MFA por TOTP)" acima), mas ativar é decisão de cada
  pessoa — não há configuração que o exija por papel ou para a operação inteira;
- **sem CRUD de usuário via API**: criar, editar, desativar e listar usuário é
  operação de banco ou CLI. A matriz aprovada não diz quem administra usuário, e
  decidir isso sem o cliente seria inventar requisito — um `POST /api/usuarios`
  aberto ao papel errado deixaria qualquer um criar um `gestor` e escalar
  sozinho.
