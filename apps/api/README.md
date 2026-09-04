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
| `GET /api/usuarios`, `POST /api/usuarios`, `PATCH /api/usuarios/{id}` | — | ✅ | — |

`conferente` está contido em `coordenador`. `gestor` **não** é superconjunto de
ninguém: é outro eixo — lê a operação inteira, não a executa, e é o único que
escreve baseline, que é dado de gestão e não de conferência.

É essa forma da matriz — eixo, e não degrau — que faz o coordenador **não** poder
criar nem promover a `gestor` em `/api/usuarios`: ver "Administração de usuários"
abaixo e o [ADR 0004](../../docs/adr/0004-administracao-de-usuarios-pela-api.md).

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

- **`tentativas_login` cresce a cada tentativa de login, sucesso ou falha.**
  O expurgo por retenção existe desde a issue #39 — ver "Retenção e expurgo de
  dados" abaixo —, mas ninguém o chama sozinho: não há agendador embutido, e
  ligar o cron (`api-retencao`) em produção é decisão de operação.
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
| `POST /api/auth/mfa/reemitir-codigos` | `{"senha": "...", "codigo": "123456"}` | `{"codigos": ["a1b2c-3d4e5", ...]}` |

`/mfa/iniciar` grava o segredo **sem ativar nada**: um segredo que o app não
guardou (QR code fechado antes da hora, celular sem bateria) não pode passar a
ser exigido no login seguinte. Chamar de novo antes de confirmar **substitui** o
segredo — quem perdeu o QR code no meio do cadastro recomeça, e um segredo não
confirmado não protege nada. Com MFA já ativado, responde 409.

`/mfa/confirmar` é o que ativa, e só com a prova de que o app guardou o segredo.
Ele devolve os **códigos de recuperação** (`MFA_CODIGOS_RECUPERACAO`, 8) — a
única vez em que *aqueles* códigos existem em claro. O banco guarda só o hash
Argon2id: não há endpoint que mostre de novo os códigos de uma emissão.

`/mfa/desativar` exige **senha e código**, os dois. Com só o código, uma sessão
sequestrada desligaria o segundo fator sozinha, que é exatamente o que ele
existe para impedir; com só a senha, bastaria a senha vazada, que é a hipótese
que faz alguém ativar MFA. Senha errada e código errado respondem o mesmo 422.

Exigir os dois não bastava sozinho, e essa foi uma lacuna real até a issue #39:
**a rota não aplicava o freio da issue #33**, então os 10⁶ códigos de seis
dígitos podiam ser sondados sem 429, sem atraso e sem deixar linha em
`tentativas_login`. Era um alvo mais barato que `/mfa/verificar`, que sempre foi
protegida, com prêmio maior — lá o sucesso dá uma sessão, aqui desliga o segundo
fator. Hoje o bloqueio é avaliado antes de conferir qualquer credencial e a
tentativa é registrada nos dois desfechos, como em `/mfa/verificar` e
`/mfa/reemitir-codigos`. A consequência vale saber: essas linhas contam para a
trava de conta, então errar muitas vezes aqui bloqueia o login da pessoa.

Os quatro exigem **sessão de usuário**: requisição por `X-API-Key` responde 403 —
chave de máquina não tem celular nem app autenticador.

#### Reemissão dos códigos de recuperação (issue #39)

`POST /api/auth/mfa/reemitir-codigos` troca a lista inteira por uma nova, **sem
desativar o segundo fator e sem tocar no segredo TOTP** — o app autenticador
cadastrado continua valendo, e não há QR code novo para escanear. Antes dela,
quem perdia a lista tinha um caminho só: desativar e ativar de novo, com segredo
novo e app reconfigurado. O custo era alto o bastante para a pessoa adiar, e
adiar significa ficar sem a saída de emergência justamente enquanto o MFA está
ligado.

**Ela exige senha e código atual, os dois, como `/mfa/desativar`** — e pela mesma
razão, com um agravante: o que sai daqui é uma lista de credenciais que *pulam*
o segundo fator no login. Uma sessão sequestrada que pudesse pedir códigos novos
viraria acesso permanente à conta, imune à troca de senha e ao próprio MFA.
Senha errada e código errado respondem o **mesmo** 422: duas mensagens diriam a
quem está com a sessão de outra pessoa qual metade da credencial ele já tem.

O `codigo` é **só** o TOTP do app: código de recuperação não é aceito aqui. Um
código vazado que gerasse oito novos desfaria o uso único da lista inteira.

**Todos os códigos anteriores morrem, usados e não usados**, no mesmo `delete`
que precede o `insert` — e na mesma transação, para uma falha no meio não deixar
a pessoa sem código nenhum. Preservar um código antigo faria a reemissão
*aumentar* a superfície de ataque: quem troca a lista porque ela pode ter vazado
ficaria com a lista vazada valendo do mesmo jeito.

O passo TOTP aceito é gravado em `usuarios.mfa_ultimo_passo`, como em
`/mfa/verificar`: o código gasto aqui não serve para o login em seguida.

MFA não ativado responde 409 — não há lista a reemitir.

**O freio da issue #33 vale nesta rota**, consultado antes de qualquer Argon2 e
registrado nos dois desfechos, como em `/mfa/verificar`. A consequência precisa
estar dita: as falhas são gravadas em `tentativas_login` com o e-mail da pessoa,
e essas linhas contam para a trava de conta — **errar senha ou código muitas
vezes aqui bloqueia o login dela** por `LOGIN_TRAVA_MINUTOS`. É o preço de não
deixar seis dígitos serem sondados de graça numa rota que emite bypass do
segundo fator, e é o mesmo comportamento que `/mfa/verificar` já tem.

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
- **Quem perde a lista *junto com o celular* ainda precisa de quem administre o
  banco.** `/mfa/reemitir-codigos` resolve o caso de quem perdeu só a lista, e
  ele exige o código do app autenticador — sem o celular não há como provar quem
  é para reemitir, e os códigos só existem em claro no instante da emissão. Um
  caminho de recuperação que dispensasse o segundo fator seria uma porta ao lado
  da porta.
- **Errar a reautenticação de `/mfa/reemitir-codigos` tranca o login.** As
  falhas contam em `tentativas_login` com o e-mail da pessoa, como as de
  `/mfa/verificar`. É o comportamento desejado — a alternativa é deixar sondar
  seis dígitos de graça numa rota que emite bypass do segundo fator —, mas
  significa que uma sessão sequestrada consegue trancar o login de quem ela
  sequestrou, sem acertar credencial nenhuma.
- **Não há política que obrigue MFA.** Ativar é decisão de cada pessoa; não
  existe configuração que o exija por papel ou para a operação inteira. Isso é
  requisito de produto, e inventá-lo aqui trancaria gente para fora sem ninguém
  ter decidido.

### Administração de usuários

Quem administra usuário é o **coordenador** — decisão de produto tomada com o
cliente, registrada no [ADR 0004](../../docs/adr/0004-administracao-de-usuarios-pela-api.md).
Três rotas, e nenhuma mais:

| rota | o que faz |
| --- | --- |
| `GET /api/usuarios` | lista, paginada, com filtro por `ativo` |
| `POST /api/usuarios` | cria e devolve, **uma única vez**, o token de definição de senha |
| `PATCH /api/usuarios/{id}` | altera nome, papel e `ativo` |

Este é o endpoint mais perigoso da API — quem cria usuário decide quem entra — e
cada regra abaixo existe por causa de um ataque concreto.

#### O coordenador não cria nem promove a `gestor`

Papéis atribuíveis por esta API: **`conferente` e `coordenador`**. Tentar
`gestor`, na criação ou no `PATCH`, responde **403** dizendo que ele é criado por
linha de comando.

`gestor` não é um degrau acima do coordenador: é outro eixo da matriz. Um
coordenador que criasse um gestor estaria **se dando acesso a dado de gestão que
o papel dele não tem** — bastaria criar a conta e entrar nela. Recusar só na
criação deixaria a mesma escalada em dois passos, e é por isso que a promoção é
recusada junto.

#### A senha nunca passa por quem administra

A criação grava o hash de um valor **aleatório e descartado** — uma senha que
ninguém conhece, nem quem criou a conta — e devolve um token de recuperação:

```json
{
  "usuario": {"id": "…", "nome": "Ana Souza", "email": "ana@exemplo.com",
              "papel": "conferente", "ativo": true},
  "token_definicao_senha": "…"
}
```

Quem administra repassa `{FRONTEND_BASE_URL}/redefinir-senha?token=<token>` à
pessoa pelo canal que já usa, e ela escolhe a própria senha. **É a única vez em
que o token aparece** — mesma regra dos códigos de recuperação do MFA: o banco
guarda só o SHA-256 dele, e nenhum endpoint o mostra de novo.

Por que assim, e não uma senha temporária no corpo da requisição: quem administra
não deve conhecer a senha de ninguém, e uma senha escolhida pelo administrador
tende a virar um padrão (`Mudar@123`) reusado na operação inteira. O fluxo de
redefinição já existe, tem uso único e expiração.

A conta e o token entram no **mesmo commit**. Se o token não puder ser emitido
(teto de `SENHA_RESET_MAX_POR_HORA`, que só alcança uma conta nova se estiver
configurado como zero), a criação é desfeita e a resposta é **503** — uma conta
sem caminho de primeiro acesso não pode nascer em silêncio.

#### Ninguém se tranca fora, e ninguém se promove

| tentativa | resposta |
| --- | --- |
| alterar o próprio papel | 403 — é o único papel cuja alteração interessa a quem ataca |
| desativar a própria conta | 403 |
| desativar **ou rebaixar** o último coordenador ativo | 409 |

A última existe porque sem coordenador ativo não sobra quem administre usuário
nem quem edite regra, e a saída seria acesso ao banco. Ela cobre o rebaixamento
junto com a desativação — as duas esvaziam a coordenação do mesmo jeito. Com
sessão de usuário ela é defesa em profundidade (quem chama é sempre um
coordenador ativo e não pode agir sobre a própria conta, então sempre resta ele);
para a `X-API-Key`, que passa por qualquer checagem de papel e não tem "si
mesmo", ela é a única trava.

#### Desativar, nunca excluir — e desativar revoga as sessões

**Não existe `DELETE`**, e não é omissão: `log_conferencia.usuario_id` referencia
`usuarios`, e apagar uma pessoa apagaria a resposta a "quem fez esta ação?", que
é a razão de existir da issue #30.

Desativar **revoga todas as sessões abertas** da pessoa, na mesma transação.
`usuarios.ativo = false` sozinho já derruba o acesso na requisição seguinte, mas
sem a revogação uma reativação futura ressuscitaria os cookies antigos —
inclusive o de um aparelho que ela não tem mais. E sem revogar, quem foi
desligado às pressas seguiria navegando por até `SESSAO_DURACAO_HORAS` (12h) com
o cookie que já tem, que é exatamente o cenário em que se desliga alguém às
pressas.

#### Nada de credencial na resposta

Nenhuma resposta destas rotas carrega `senha_hash`, `mfa_secret` nem
`mfa_ultimo_passo`: a saída é sempre a projeção explícita `UsuarioOut`, a mesma
do login e do `GET /api/auth/eu` — nunca o model serializado. E-mail duplicado
responde **409 com mensagem neutra**, sem dizer nome nem papel de quem já está
cadastrado: uma sessão de coordenador comprometida não pode virar oráculo de
enumeração.

#### Auditoria administrativa

Toda criação, alteração, desativação e reativação de usuário grava um evento
em `auditoria_usuarios` — quem fez, em quem, o que mudou (valor anterior e
novo) e quando — na **mesma transação** da mutação (issue #30, fecha o ponto
que o [ADR 0004](../../docs/adr/0004-administracao-de-usuarios-pela-api.md)
deixou aberto). `GET /api/usuarios/auditoria` lê esse histórico, paginado, do
evento mais recente para o mais antigo, filtrável por usuário-alvo (e por ator
e por ação); é do coordenador, como as três rotas acima, mas vive em router
próprio — ele não conta como uma quarta rota de `/api/usuarios`.

Chamada por `X-API-Key` também é auditada: o ator sai com rótulo `"api"` e sem
`usuario_id`, porque não existe "si mesmo" para a chave — a mesma convenção de
`log_conferencia`. Um `PATCH` que não muda nada de fato (reenvia o valor que
já está no banco) não grava evento — nada mudou. Nenhuma resposta e nenhuma
linha desta tabela carregam `senha_hash`, `mfa_secret`, `mfa_ultimo_passo` ou
token algum.

**Sem política de retenção.** A tabela cresce a cada operação administrativa e
não há expurgo nesta entrega. Não é descuido: por quanto tempo guardar auditoria
que tem e-mail dentro é decisão de negócio e de LGPD, não de engenharia — e uma
retenção escolhida no chute apagaria justamente a linha que uma investigação
procura. Registrado nas limitações conhecidas, abaixo.

#### Limitações honestas

- **O token de definição de senha expira em `SENHA_RESET_VALIDADE_MINUTOS` (30),
  e não há rota para reemiti-lo.** Se o administrador demorar a repassá-lo, a
  pessoa precisa pedir um link em `POST /api/auth/senha/esqueci` — que depende de
  SMTP configurado. Sem SMTP, o caminho volta a ser o CLI.
- **Uma operação com um único coordenador não consegue desligá-lo pela API.** O
  sistema recusa a alteração que deixaria zero coordenador ativo, mas nada obriga
  a existir dois. Criar um segundo coordenador antes é o caminho.
- **Rebaixar um gestor é possível e não é reversível pela API.** O `PATCH` recusa
  *atribuir* `gestor`, não *alterar* quem já é: um coordenador pode mover um
  gestor para `conferente`, e devolvê-lo ao papel exige o CLI. A alternativa
  reversível, quando a intenção é só tirar o acesso, é `ativo: false`.

### Criar o primeiro usuário

O CLI continua sendo o caminho para o **primeiro** acesso — não há quem
administre antes do primeiro coordenador — e para criar **gestor**, que a API
não atribui (ver "Administração de usuários" acima):

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

## Rate limit das rotas caras

Quatro rotas de `/api/*` custam desproporcionalmente mais que as outras 22, e
uma delas custa **dinheiro**. Desde o
[ADR 0005](../../docs/adr/0005-rate-limit-das-rotas-caras.md) (issue #39) elas —
e só elas — têm limite de requisições **por identidade de quem chama**:

| rota | por que entra | pessoa/hora | máquina/hora |
| --- | --- | :-: | :-: |
| `POST /api/documentos` | dispara extração por IA **síncrona** dentro da requisição: cada upload é uma chamada paga | 120 | 600 |
| `GET /api/relatorios/conferencia.csv` | o extrato inteiro do filtro, sem paginação | 20 | 60 |
| `GET /api/documentos/{id}/arquivo` | streaming que ocupa um worker enquanto transmite (ADR 0003) | 600 | 600 |
| `POST /api/alertas/varredura` | dispara os detectores e envia WhatsApp de verdade | 30 | 600 |

As demais rotas — as leituras paginadas, com teto de `limite <= 200`, e as
escritas de uma linha — **continuam sem limite nenhum**, e isso é decisão, não
pendência: cobrar de 22 rotas baratas o custo de proteger 4 caras é custo certo
pago contra risco hipotético. Estender o limite depois é barato; desfazer um
custo já cobrado de todas as rotas não é.

### A chave é a identidade, nunca o IP

O contador é por `usuarios.id` para pessoa e pela chave `maquina:api` para a
integração autenticada por `X-API-Key`. **Duas conferentes trabalhando lado a
lado não competem pelo mesmo contador** — que é exatamente o que um limite por
IP faria: `CONFIAR_EM_X_FORWARDED_FOR` tem default `false` e há proxy em
produção, então ou todo mundo chega com o IP do balanceador (a primeira pessoa a
exportar dois relatórios travaria a equipe), ou o header é aceito sem allowlist
e qualquer cliente forja um IP novo por requisição.

A chave de máquina tem limite próprio e mais folgado: o padrão de uso dela é
legítimo e repetitivo. **O cron da varredura não é afetado** — ele chama
`python -m homecareos.alerts.scan` (o módulo, via
`docker compose run --rm api-alertas`), que não faz requisição HTTP nenhuma. O
limite folgado da máquina existe porque nada garante que alguém não tenha
apontado um agendador para a rota.

### O 429

Estourou, a resposta é **429 com `Retry-After` em segundos**, no envelope de
erro padrão. Duas diferenças em relação ao 429 do login:

- **a mensagem diz qual recurso foi limitado.** O 429 do login é genérico de
  propósito, para não virar oráculo de "esta conta existe"; aqui quem chegou já
  está autenticado como si mesmo, e esconder qual limite estourou só atrapalha
  quem precisa se corrigir;
- **o `Retry-After` é calculado**, não fixo: é a janela menos a idade do consumo
  mais antigo dentro dela — o instante em que a cota de fato volta. Um valor
  inflado ensina a pessoa a ignorar o header.

A requisição bloqueada **não** executa o trabalho caro: o freio é uma dependency
que roda antes do handler. Um 403 por papel também não consome cota — a
autorização é avaliada antes.

### O consumo é registrado antes de executar, e é escolha consciente

Uma requisição que depois falhe na validação (um upload com tipo de arquivo
inválido, por exemplo) terá consumido cota sem ter custado a chamada de IA. É o
lado conservador do erro: registrar só no sucesso deixaria um laço de
requisições inválidas passar livre — e é justamente o laço que se quer conter.

### Configuração

Uma variável por recurso e por tipo de principal, com janela de **1 hora** para
os quatro (constante em `limites/protecao.JANELA`, como as outras duas janelas
de uma hora do projeto):

```bash
LIMITE_UPLOAD_DOCUMENTO_PESSOA_POR_HORA=120
LIMITE_UPLOAD_DOCUMENTO_MAQUINA_POR_HORA=600
LIMITE_RELATORIO_CSV_PESSOA_POR_HORA=20
LIMITE_RELATORIO_CSV_MAQUINA_POR_HORA=60
LIMITE_DOWNLOAD_ARQUIVO_PESSOA_POR_HORA=600
LIMITE_DOWNLOAD_ARQUIVO_MAQUINA_POR_HORA=600
LIMITE_VARREDURA_ALERTAS_PESSOA_POR_HORA=30
LIMITE_VARREDURA_ALERTAS_MAQUINA_POR_HORA=600
```

**Os oito números são ASSUNÇÃO deste time, não requisito medido**, e o ADR
registra o porquê: calibrar sem medir uso real produz número inventado com cara
de decisão. Eles nascem folgados de propósito — uma conferente processando sem
parar não passa de algumas dezenas de uploads por hora, e o download é o gesto
mais frequente da conferência (o limite dele existe para conter laço, não uso).
**A primeira calibragem precisa olhar dado de uso real**, e o pior desfecho de
um limite mal posto não é o abuso que passa: é uma conferente bloqueada no meio
do turno, que não abre chamado dizendo "recebi 429" e sim que o sistema parou.

### Onde o contador vive

Numa tabela nova, `consumos_rate_limit`, com **uma linha por consumo** e
contagem por `COUNT` sobre a janela — mesmo desenho de `tentativas_login`. A
tabela guarda só `chave`, `recurso` e `created_at`: nenhum e-mail, nenhum token,
nenhuma chave de API.

No Postgres e não em memória do processo, e a razão é o modo de falha: a API
sobe hoje como processo uvicorn único, e um contador em memória funcionaria —
mas nada no repositório documenta quantas instâncias rodam em produção, e no dia
em que alguém acrescentar uma réplica o contador em memória **dobra o limite em
silêncio**, sem erro e sem teste vermelho. Redis é a resposta tecnicamente
certa e foi descartada **por ora** (dependência de infraestrutura nova, custo
permanente cobrado agora): quando a API passar a rodar em mais de uma instância,
o ADR 0005 deve ser substituído, não remendado.

**Isto não é proteção contra DDoS** e não deve ser vendido como tal. Ataque
volumétrico chega antes da aplicação e é trabalho da borda (proxy, CDN, WAF),
que este repositório não descreve. O que este freio contém é abuso de uso
legítimo: script mal escrito, integração em laço, curiosidade cara.

## Retenção e expurgo de dados

Três tabelas crescem para sempre e não são só log — `tentativas_login`,
`tokens_recuperacao` e `alertas_enviados` são consultadas por freios de
segurança ativos, dentro de janelas de tempo (issue #39). `alertas_enviados`
tem ainda um motivo de privacidade: `mensagem` guarda o texto exatamente como
foi enviado, **incluindo o nome do paciente** (ver `db/models/alerta.py`) —
dado pessoal de saúde retido para sempre não é neutro, é exposição que só
cresce.

| tabela | quem consulta | janela de segurança | o que quebra se você apagar dentro dela |
| --- | --- | --- | --- |
| `tentativas_login` | trava de IP e de conta (`auth/protecao.avaliar_bloqueio`) | `LOGIN_JANELA_MINUTOS` (15 min por padrão) | o contador de falhas cai e o atacante ganha tentativas de volta |
| `tokens_recuperacao` | teto de emissão (`auth/recuperacao.emissoes_recentes`) | `JANELA_DO_TETO`, 1h, **hardcoded** em `auth/recuperacao.py` | o teto de `SENHA_RESET_MAX_POR_HORA` afrouxa |
| `alertas_enviados` | cooldown (`alerts/repository.existe_envio_recente`) | `ALERTAS_COOLDOWN_HORAS` (24h por padrão) | o mesmo alerta dispara de novo |
| `alertas_enviados` | rate limit (`alerts/repository.contar_envios_desde`) | `JANELA_RATE_LIMIT`, 1h, **hardcoded** em `alerts/service.py` | o teto por destinatário afrouxa |

Por isso o expurgo **recusa-se a rodar** quando a retenção configurada for
menor que **o dobro** da janela de segurança ativa de uma tabela — a janela é
o piso absoluto; a margem existe porque rodar exatamente nela é apostar
contra o relógio (job atrasado, retenção configurada minutos acima do
limite). O erro diz qual janela foi violada e qual o mínimo aceitável, e
nada é apagado (nem nas outras tabelas da mesma execução).

### Configuração

| variável | default | por quê |
| --- | --- | --- |
| `RETENCAO_TENTATIVAS_LOGIN_DIAS` | 180 | registro de acesso à aplicação; 6 meses é o horizonte que o Marco Civil da Internet (Lei 12.965/2014, art. 15) estabelece para provedor de aplicações com fins econômicos |
| `RETENCAO_TOKENS_RECUPERACAO_DIAS` | 30 | o valor de auditoria é curto; o token em si já morre em `SENHA_RESET_VALIDADE_MINUTOS` (30 min) |
| `RETENCAO_ALERTAS_ENVIADOS_DIAS` | 90 | `mensagem` contém nome de paciente — reter menos é a decisão mais segura, desde que fique muito acima do cooldown de 24h |
| `RETENCAO_TAMANHO_LOTE` | 1000 | tamanho do lote de apagar, com commit por lote |

**Os três defaults de dias são uma assunção deste time, não um requisito
confirmado pelo cliente ou pelo jurídico** — precisam de confirmação antes de
valer como política real de retenção.

### Um token ainda válido nunca é apagado por idade

`tokens_recuperacao` tem uma regra além da idade: um token **ainda válido e
não usado** (`used_at IS NULL AND expires_at > agora`) nunca é apagado, mesmo
que `created_at` já tenha passado da retenção configurada — apagar esse token
quebraria o link que está na caixa de e-mail de alguém, no meio do clique.

### O comando

```bash
# Conta e reporta, sem apagar nada (padrão — precisa de --executar para valer):
docker compose run --rm api-retencao

# Apaga de verdade:
docker compose run --rm api-retencao python -m homecareos.retencao.cli --executar

# Uma tabela só:
docker compose run --rm api-retencao python -m homecareos.retencao.cli \
  --tabela alertas_enviados --executar
```

Sem `--executar` o comando só CONTA e reporta — é o padrão, de propósito: a
primeira execução contra um banco de produção precisa ser uma decisão
informada, não um salto no escuro. O resumo em JSON sempre diz
`"dry_run": true` ou `false` explicitamente, então mesmo um cron que rodasse
em dry-run por engano é auditável em segundos a partir do próprio log.

O resumo diz, por tabela, quantas linhas saíram (ou sairiam, em dry-run) e
qual foi a data de corte usada:

```json
{"dry_run": true, "executado_em": "...", "tabelas": {"tentativas_login": {"apagadas": 42, "corte": "..."}}}
```

Código de saída `1` quando a retenção viola a janela mínima de alguma tabela
ou quando um argumento é inválido; `0` em qualquer expurgo bem-sucedido
(real ou dry-run), mesmo com zero linhas apagadas.

### Em lotes, e por isso não atômico

O expurgo apaga em lotes (`RETENCAO_TAMANHO_LOTE`, commit a cada lote) — um
`DELETE` único sobre anos de dado acumulado seguraria locks e cresceria o WAL
de uma vez só, numa tabela (`tentativas_login`) que recebe insert a cada
login. **Consequência**: a operação inteira não é atômica. Uma interrupção no
meio deixa parte apagada — para expurgo por idade isso é aceitável, porque a
próxima execução termina o serviço, mas é bom saber disso antes de perguntar
por que só apagou metade.

### Ninguém liga o cron automaticamente

Esta entrega **não** inclui agendador nenhum (nem embutido, nem cron dentro
do container) — mesma decisão de `api-alertas`: em produção quem chama
`api-retencao` é um cron **externo**, não o `up`. Ligar esse cron em produção
é decisão de operação, com aprovação humana.

### Follow-ups conhecidos

- **Sem índice líder em `created_at`.** Nenhuma das três tabelas tem
  `created_at` como coluna líder de índice, então o `DELETE` por data tende a
  seq scan. Não foi criado índice nesta entrega — indexar antes de medir é
  otimização prematura para um job de manutenção fora do caminho de request, e
  todo índice novo custa escrita numa tabela que recebe insert a cada login.
  Se o expurgo diário demorar demais em regime, medir e considerar índice em
  `created_at`.
- **A tabela de auditoria administrativa não tem retenção aqui.** Ela ainda
  não existia nesta branch quando esta entrega foi feita; quando existir,
  decidir a política de retenção dela é trabalho à parte.
- **`consumos_rate_limit` também não tem retenção ainda.** A tabela do freio do
  ADR 0005 ("Rate limit das rotas caras", acima) cresce a cada consumo das
  quatro rotas limitadas e **precisa entrar nesta política**, com janela mínima
  respeitando a janela do limite (1 hora, `limites/protecao.JANELA`) — a mesma
  trava que protege `tentativas_login` de ser expurgada dentro da janela do
  freio de login. A ligação não foi feita nesta entrega porque
  `homecareos.retencao` estava sendo reescrito em paralelo; até ela existir,
  ninguém apaga essas linhas.

### Limitações conhecidas

Esta entrega **não** tem, e é deliberado — cada um destes itens é decisão de
produto/segurança que merece a sua própria issue, e uma versão frouxa seria pior
que a ausência:

- **sem rate limit geral da API**: existem dois freios, e nenhum deles cobre
  `/api/*` inteiro. O da issue #33 cobre as quatro rotas do fluxo de
  autenticação (ver "Bloqueio por tentativa de login" acima) e o do ADR 0005
  cobre as quatro rotas caras, por identidade (ver "Rate limit das rotas caras"
  acima). As demais rotas — leituras paginadas com teto de `limite <= 200` e
  escritas de uma linha — continuam sem limite de requisições, **por decisão**:
  cobrar de todas o custo de proteger quatro seria custo certo contra risco
  hipotético. Nenhum dos dois é proteção contra DDoS;
- **recuperação de senha só com SMTP configurado**: ela existe desde a issue #34
  (ver "Recuperação de senha por e-mail" acima), mas sem `SMTP_HOST`/
  `SMTP_REMETENTE` fica desligada e o caminho volta a ser o CLI;
- **sem política que obrigue MFA**: o segundo fator existe desde a issue #35
  (ver "Segundo fator (MFA por TOTP)" acima), mas ativar é decisão de cada
  pessoa — não há configuração que o exija por papel ou para a operação inteira;
- **sem retenção da auditoria administrativa**: `auditoria_usuarios` (issue #30,
  ver "Auditoria administrativa" acima) não tem expurgo nem política de
  retenção nesta entrega — cresce indefinidamente até essa decisão existir;
- **sem criação de `gestor` pela API**: ele continua sendo criado por
  `python -m homecareos.auth.cli criar`, que exige acesso ao servidor. Não é
  lacuna: `gestor` é outro eixo da matriz, e deixá-lo atribuível por quem
  coordena seria escalada de privilégio disfarçada de conveniência.
