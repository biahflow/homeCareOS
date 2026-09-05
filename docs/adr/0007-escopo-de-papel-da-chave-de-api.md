# ADR 0007 — Escopo de papel da `X-API-Key`

- **Status:** aceito
- **Data:** 2026-09-05
- **Relacionado:** [ADR 0001](0001-autenticacao-de-usuario-na-api.md), que criou a matriz de
  papéis e registrou a coexistência com a chave; [ADR 0004](0004-administracao-de-usuarios-pela-api.md),
  cuja seção "o que fica em aberto" nomeia este estreitamento como pendente;
  [ADR 0005](0005-rate-limit-das-rotas-caras.md), que repetia a justificativa corrigida aqui

## Contexto

`auth/dependencies.exigir_papel` deixa `Principal(tipo="maquina")` passar em
**qualquer** papel exigido. Na prática, autenticar e autorizar são a mesma
pergunta para a máquina: quem tem uma chave de `API_KEYS` faz tudo o que a API
faz — lê prontuário clínico, edita regra de glosa, escreve baseline, cria
usuário e promove quem quiser a coordenador.

Isso foi decisão consciente do ADR 0001 (item 3), e não descuido. O que mudou é
que a justificativa registrada dela **não se sustenta**.

### A justificativa era falsa, e estava escrita em quatro lugares

A afirmação, com pequenas variações, era "o cron de alertas
(`python -m homecareos.alerts.scan`) depende da chave":

| lugar | o que dizia |
| --- | --- |
| `auth/dependencies.exigir_papel` | "o cron de alertas (…) e todas as integrações existentes dependem disso" |
| `apps/api/README.md`, tabela de credenciais | "é o que o cron `python -m homecareos.alerts.scan` usa" |
| `docs/adr/0005`, §"a identidade" | "é o cron da varredura de alertas" |
| `tests/test_autorizacao_papeis.py` | "o cron (…) depende dela" |

**O cron não usa a chave.** `alerts/scan.py` abre `get_sessionmaker()` e fala
direto com o Postgres: não há uma única requisição HTTP à própria API no
caminho, e portanto não há header nenhum para mandar. O único HTTP que a
varredura faz é para o gateway de WhatsApp (`alerts/uazapi.py`), que tem
credencial própria. O mesmo já estava registrado corretamente em dois outros
lugares — `config.py`, no comentário do limite de varredura, e o README na seção
de rate limit ("**o cron da varredura não é afetado**") —, o que torna a
contradição interna, e não uma dúvida sobre o comportamento.

Nenhum outro consumidor no repositório manda o header: o frontend fala com a
API por cookie de sessão através do proxy do Next ([ADR 0002](0002-proxy-do-frontend-para-a-api.md)),
e não há um `X-API-Key` sequer sendo enviado em `apps/web/` ou
`packages/contracts/` — `lib/sessao.ts` chega a anotar que "nenhum navegador
chega aqui assim". A operação confirmou que não existe, hoje, integração externa
usando a chave.

Sobrou uma chave-mestra sustentada por um consumidor que não existe.

## Decisão

O que a `X-API-Key` abre passa a ser **declarado**, em `API_KEY_PAPEIS`.

**1. Uma lista de papéis, separada por vírgula.** `Settings.api_key_papeis`, no
mesmo formato de `api_keys`. `exigir_papel(*papeis)` deixa a máquina passar
quando algum papel declarado está entre os exigidos pela rota, e responde 403
quando não está. Para sessão de usuário nada muda: o papel continua sendo o da
pessoa, e a matriz do ADR 0001 continua intacta.

**2. Vazio é o default, e o default é restritivo.** Sem `API_KEY_PAPEIS`, a
chave autentica e não abre rota de papel restrito nenhuma. `API_KEY_PAPEIS=conferente,coordenador,gestor`
reproduz exatamente o acesso que ela tinha antes deste ADR — a diferença é que
agora isso é uma decisão escrita por quem opera, e não o estado em que o sistema
nasce.

**3. 401 e 403 continuam sendo respostas diferentes.** Chave ausente ou errada é
401 com `MENSAGEM_CREDENCIAL_INVALIDA`, indistinguível de cookie inválido — a
decisão do ADR 0001 sobre não entregar informação a quem sonda não é tocada.
Chave **válida** sem o papel é 403 com `MENSAGEM_SEM_PERMISSAO`, a mesma
mensagem que uma pessoa sem papel recebe, e que continua sem nomear o papel que
faltou. Confundir os dois status transformaria uma decisão de autorização em
sinal sobre a validade do segredo.

**4. Papel escrito errado derruba o boot, em qualquer ambiente.** Um typo e o
default restritivo produziriam o **mesmo** efeito em runtime — 403 em tudo — e a
diferença entre os dois é justamente o que quem configurou precisa saber. A
recusa mora em `main._validar_configuracao_de_auth`, junto da que já existe para
`api_keys` vazio fora de `local`, e a mensagem lista os papéis válidos.

A validação **não** é um `field_validator` de `Settings`, por duas razões. A
primeira é de camada: `config` é a mais baixa do projeto e não conhece `Papel`
— validar lá exigiria repetir a lista de papéis, criando um segundo lugar para
ela envelhecer. A segunda é de raio de alcance, e é a mesma que `alerts/config.py`
já documenta: `Settings` também é construída por `alerts/scan.py` e
`retencao/cli.py`, e um erro de digitação numa configuração de **autorização da
API** não pode derrubar o cron que avisa a operação. Em
`_validar_configuracao_de_auth` a recusa alcança só o processo que serve
`/api/*`, que é exatamente onde ela importa.

## Consequências

**O que melhora.** A pergunta "o que esta chave pode fazer?" passa a ter resposta
numa variável, e não em "tudo". Uma integração que só lê relatório pode ser
declarada `conferente` e deixa de conseguir promover alguém a coordenador. E o
sistema deixa de nascer com uma credencial de acesso total ativa por omissão —
que era a forma mais barata de um ambiente novo vazar prontuário por descuido de
configuração.

**O que custa.** Mais uma variável para lembrar em cada ambiente. Quem subir um
ambiente com integração de verdade e esquecer `API_KEY_PAPEIS` vai ver 403 em
toda chamada de máquina, e a resposta **não** vai dizer que falta configuração —
`MENSAGEM_SEM_PERMISSAO` não nomeia papel, de propósito. O custo é consciente: o
caminho para descobrir é o `.env.example` e o README, e o desfecho errado
(integração parada, visível no primeiro minuto) é preferível ao outro (chave
aberta em tudo, invisível até o incidente).

**O que fica em aberto.** O escopo é **por instalação**, não por chave: com duas
chaves em `API_KEYS` para rotação, as duas carregam os mesmos papéis. Escopar
integração a integração é o próximo passo, e está descrito abaixo.

## Alternativas consideradas

**Mapa chave→papel (`API_KEYS=chave:papel,...`).** É a alternativa mais granular
e a que um dia deve substituir esta: cada integração com o seu escopo, revogável
sozinha. Descartada por ora por duas razões. Não há integração para escopar — o
levantamento acima não encontrou nenhum consumidor da chave —, e escopo por
chave sem chave para escopar é estrutura construída para um requisito
hipotético. E ela mexeria no formato de `API_KEYS`, que é lido pela comparação
em tempo constante de `api/auth.py`: acomodar sintaxe nova ali é mexer na parte
crítica da autenticação para resolver um problema que ainda não existe. O ADR
0001 já registrou "chave de API por usuário" como o caminho quando houver
cadastro de integração; este ADR não fecha essa porta, só não a atravessa antes
da hora.

**Manter a chave-mestra e corrigir só a documentação.** Seria o menor diff: o
comportamento continua, e as quatro afirmações falsas viram uma verdadeira ("a
chave dá acesso total porque foi assim que ela nasceu"). Descartada porque a
justificativa era o que sustentava a decisão. Sem o cron, sobra "acesso total
por inércia" — que é o tipo de decisão que ninguém tomou e todo mundo herda.

**Um papel único (`API_KEY_PAPEL=coordenador`).** Mais simples de configurar e
de explicar. Descartada pela forma da matriz do ADR 0001: `gestor` não é
superconjunto de ninguém, é outro eixo. Um papel só obrigaria a escolher entre
"lê a operação inteira" e "executa a conferência", e uma integração legítima
pode precisar dos dois. A lista também é a forma que casa com a assinatura de
`exigir_papel(*papeis)`, sem tradução no meio.

**Remover a `X-API-Key`.** Sem consumidor conhecido, apagar a credencial seria
defensável. Descartada: a decisão do ADR 0001 (item 3) de ter uma credencial
máquina-a-máquina continua correta, e a operação pode precisar dela sem aviso
prévio. O problema levantado aqui é de escopo, não de existência — e remover uma
credencial é decisão de outra natureza, com migração de quem a use.
