# ADR 0005 — Rate limit das rotas caras, por identidade

- **Status:** aceito
- **Data:** 2026-09-04
- **Issue:** #39
- **Relacionado:** [ADR 0001](0001-autenticacao-de-usuario-na-api.md), que introduziu a
  identidade em que este ADR se apoia; [ADR 0003](0003-servir-o-documento-escaneado.md),
  que registrou o custo de servir o arquivo

## Contexto

O `apps/api/README.md` registra entre as limitações conhecidas:

> **sem rate limit geral da API**: o freio da issue #33 cobre só
> `POST /api/auth/login`; as demais rotas de `/api/*` não têm limite de requisições.

Isso está desatualizado em um detalhe e certo no essencial. O freio de `auth/protecao.py`
hoje cobre quatro rotas, todas do fluxo de autenticação — `POST /api/auth/login`,
`/mfa/verificar`, `/mfa/desativar` e `/mfa/reemitir-codigos`. As outras **26 rotas** de
`/api/*` não têm limite nenhum.

Antes de propor qualquer coisa, três fatos do levantamento mudaram o desenho — e o
terceiro inverte a solução óbvia.

### 1. As rotas não custam a mesma coisa, e uma delas custa dinheiro

`POST /api/documentos` lê o arquivo inteiro em memória, grava no S3/MinIO **e dispara a
extração por IA de forma síncrona dentro da própria requisição**
(`intake/service.py:237` → `extraction/dispatcher.py:37-60` → chamada ao provider
Anthropic). Cada upload é uma chamada paga a um provider externo.

Um laço de upload não derruba a API por CPU: ele consome orçamento. É a única rota do
sistema em que o abuso tem custo em dinheiro, e é a que mais precisa de freio.

Logo atrás vêm `GET /api/documentos/{id}/arquivo` (streaming do storage, e o ADR 0003 já
registra que "cada documento aberto ocupa um worker enquanto transmite"),
`GET /api/relatorios/conferencia.csv` (o extrato inteiro do filtro, sem paginação) e
`POST /api/alertas/varredura` (dispara os detectores e fala com o gateway de WhatsApp).

As demais 22 rotas são leituras paginadas com teto de `limite <= 200`, ou escritas de uma
linha. Tratá-las igual às quatro acima seria cobrar de todo mundo o preço de um problema
que só existe em quatro lugares.

### 2. O freio atual custa caro por requisição

`avaliar_bloqueio` faz até três `SELECT` no Postgres (falhas por IP, último sucesso do
e-mail, falhas do e-mail), e `registrar_tentativa` acrescenta um `INSERT`. Numa listagem
paginada, que hoje faz duas queries, aplicar o mesmo mecanismo **dobraria ou triplicaria
o custo da rota** — para conter um abuso que ninguém observou ainda.

Um freio que torna o sistema lento para todos, o tempo inteiro, é um custo certo pago
contra um risco hipotético.

### 3. O IP é a pior chave disponível aqui

`ip_do_request` (`auth/protecao.py:61-80`) tem `confiar_em_x_forwarded_for` com default
**`false`**, e o `apps/api/README.md:239` confirma que existe proxy em produção. Nessa
configuração, **todas as requisições chegam com o mesmo IP: o do proxy.**

Um rate limit por IP nesse cenário tem dois desfechos, os dois ruins:

- **flag desligada:** a operação inteira compartilha um contador, e a primeira pessoa que
  exportar dois relatórios seguidos bloqueia a equipe toda;
- **flag ligada:** o header `X-Forwarded-For` é aceito sem allowlist de proxy nem
  verificação de origem (é confiar em todo o header ou em nenhum), então qualquer cliente
  pode forjar um IP novo por requisição — o limite deixa de existir para quem quer burlá-lo,
  e continua valendo para quem não sabe que ele existe.

O IP funciona no login porque ali **não há outra chave**: quem tenta logar ainda não é
ninguém. Nas 26 rotas restantes há uma identidade real e verificada.

## Decisão

**Rate limit por identidade, aplicado às rotas caras, com contador no Postgres.** Não um
limite uniforme sobre `/api/*`.

Três escolhas, cada uma respondendo a um dos fatos acima.

### A chave é o principal, não o IP

Toda rota fora do fluxo público de autenticação já resolve um `Principal`
(`auth/schema.py`), que traz `usuario_id` para pessoa e o rótulo `"api"` para a chave de
máquina. Essa é a chave do contador.

Ela é melhor que o IP em todos os aspectos que importam aqui: não depende de proxy, não é
forjável (vem do cookie de sessão validado contra o banco), sobrevive a rede móvel
trocando de IP no meio do turno, e é a mesma identidade que a auditoria já registra — o
que torna "quem estourou o limite" uma pergunta respondível.

As rotas públicas (`/login`, `/senha/esqueci`, `/senha/redefinir`) ficam **fora** deste
ADR: as duas primeiras já têm freio próprio (o de tentativas e o teto de três emissões por
hora), e a terceira consome um token de uso único e curta validade.

A chave de máquina (`X-API-Key`) recebe limite próprio, e mais folgado: é o cron da
varredura de alertas, cujo padrão de uso é legítimo e repetitivo.

### O escopo é a rota cara, não a API inteira

Entram no freio, nesta ordem de prioridade:

| rota | por que |
| --- | --- |
| `POST /api/documentos` | chamada paga ao provider de IA por requisição |
| `GET /api/relatorios/conferencia.csv` | extrato inteiro, sem paginação |
| `GET /api/documentos/{id}/arquivo` | streaming que ocupa um worker |
| `POST /api/alertas/varredura` | dispara envio real de WhatsApp |

As leituras paginadas comuns **não** entram. Se a operação começar a sofrer abuso nelas, o
limite se estende — mas estender é barato, e desfazer um custo cobrado de todas as rotas
não é.

### O contador vive no Postgres, e não em memória

A API sobe hoje como **processo uvicorn único**, sem `--workers` e sem gunicorn (verificado
no `Dockerfile:55` e no `docker-compose.yml:67`). Nessa topologia, um contador em memória
funcionaria e custaria zero.

Mesmo assim, não é o que este ADR propõe — e a razão é o modo de falha, não o desempenho.
**Nada no repositório documenta quantas instâncias da API rodam em produção.** Se um dia
alguém acrescentar uma réplica, um contador em memória não quebra: ele **dobra o limite em
silêncio**. Ninguém recebe erro, nenhum teste falha, e a proteção evapora sem deixar
rastro — exatamente o tipo de falha que só se descobre depois que ela custou alguma coisa.

O Postgres é a fonte de verdade compartilhada que o projeto já tem. Nas quatro rotas
escolhidas, uma ou duas queries a mais são irrelevantes: elas já falam com storage, com
provider de IA ou com gateway externo. O custo que seria inaceitável nas leituras baratas
é ruído nas rotas caras.

## Consequências

- Quem estourar o limite recebe **429 com `Retry-After`**, no mesmo formato que o login já
  usa. A mensagem diz qual recurso foi limitado — ao contrário do 429 do login, que é
  deliberadamente genérico para não servir de oráculo. Aqui não há o que esconder: quem
  chegou até a rota já está autenticado como si mesmo.
- **O limite é por pessoa.** Duas conferentes trabalhando lado a lado não competem pelo
  mesmo contador, o que é justamente o que um limite por IP faria atrás do proxy.
- A tabela do contador cresce e **precisa entrar na política de retenção** que já existe
  (`homecareos.retencao`), com janela mínima respeitando a maior janela de limite
  configurada — a mesma trava que protege `tentativas_login` de ser expurgada dentro da
  janela do freio.
- **Isto não é proteção contra DDoS**, e não deve ser vendido como tal. Um ataque
  volumétrico chega antes da aplicação; conter isso é trabalho da borda (proxy, CDN, WAF),
  que este repositório não descreve. O que este ADR contém é abuso de uso legítimo:
  script mal escrito, integração em laço, curiosidade cara.
- Um cliente autenticado que queira burlar o limite pode criar contas — se tiver o papel
  de coordenador. É aceitável: quem administra usuários já pode fazer coisas piores, e a
  administração é auditada desde o ADR 0004.
- Um limite mal calibrado atrapalha trabalho real. Os valores nascem folgados e
  configuráveis, e a primeira calibragem depende de medir o uso real — que hoje ninguém
  mediu.

## Alternativas consideradas

**Rate limit uniforme sobre todo `/api/*`, por middleware.** É a forma mais comum, e
`main.py` não tem middleware nenhum hoje, então entraria limpo. Descartada por dois
motivos concretos: cobra de 22 rotas baratas o custo de proteger 4 caras, e um middleware
único força uma chave e um limite únicos, quando o upload que chama a IA e a listagem de
operadoras não têm nada em comum além do prefixo da URL.

**Contador em memória do processo.** Custa zero por requisição e funciona na topologia
atual, que é de processo único e verificável. Descartada pelo modo de falha descrito
acima: a proteção some silenciosamente quando alguém escalar horizontalmente, e essa
decisão será tomada por outra pessoa, em outro dia, sem ler este ADR.

**Redis como store de contador.** É a resposta tecnicamente certa: correta sob múltiplas
instâncias e barata por requisição, sem tocar no Postgres. Descartada **por ora**, não por
mérito: introduzir uma dependência de infraestrutura nova traz serviço para operar,
monitorar, fazer backup e manter no Compose e em produção — custo permanente, cobrado
agora, para um ganho que só aparece em volume que a operação ainda não tem. Vale
reconsiderar quando a API passar a rodar em mais de uma instância, ou quando o limite
precisar cobrir as rotas de leitura. **Quando isso acontecer, este ADR deve ser
substituído, não remendado.**

**Rate limit por IP, como o login faz.** Descartada pelo fato 3: atrás do proxy do
projeto, ou trava a equipe inteira junto ou não trava ninguém que queira burlar.

**Não fazer nada.** Defensável enquanto o sistema roda para uma operação pequena e
conhecida, e é o estado atual. Deixa de ser defensável por causa de `POST /api/documentos`
especificamente: a rota converte requisição em gasto com provider de IA, e essa é a única
parte do sistema onde a ausência de limite tem preço direto e imediato.

## O que fica em aberto

- **A topologia de produção não está documentada em lugar nenhum do repositório** —
  número de instâncias, orquestrador, proxy de borda. Este ADR não depende dessa resposta
  (é justamente por não tê-la que o contador vai para o Postgres), mas ela precisa existir,
  e provavelmente merece um ADR de infraestrutura próprio.
- **Os limites numéricos não estão fixados aqui de propósito.** Calibrar sem medir o uso
  real produziria número inventado com aparência de decisão. Eles entram como configuração,
  com defaults folgados, e a primeira revisão deve olhar dado de uso.
- `CONFIAR_EM_X_FORWARDED_FOR` continua sem allowlist de proxy. Não afeta esta decisão
  (que não usa IP), mas continua afetando o freio do login, e merece a sua própria issue.
