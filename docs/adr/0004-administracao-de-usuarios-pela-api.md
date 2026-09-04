# ADR 0004 — Administração de usuários pela API

- **Status:** aceito
- **Data:** 2026-09-04
- **Issue:** #30
- **Relacionado:** [ADR 0001](0001-autenticacao-de-usuario-na-api.md), cuja seção
  "O que fica em aberto" é o que este ADR fecha

## Contexto

O ADR 0001 introduziu usuário, sessão e papéis, e deixou uma pergunta
explicitamente em aberto: *"A matriz de papéis acima é proposta. Confirmar com o
cliente **antes** de codar autorização"*. Uma consequência disso foi que criar,
editar, desativar e listar usuário ficou fora da API — o `apps/api/README.md`
registrava a lacuna entre as limitações conhecidas, com a razão:

> **sem CRUD de usuário via API**: (…) a matriz aprovada não diz quem administra
> usuário, e decidir isso sem o cliente seria inventar requisito — um
> `POST /api/usuarios` aberto ao papel errado deixaria qualquer um criar um
> `gestor` e escalar sozinho.

O caminho até aqui era `python -m homecareos.auth.cli criar`, que exige acesso ao
servidor. Isso funciona para o primeiro acesso e não funciona para operação: cada
pessoa que entra ou sai da equipe vira um chamado para quem tem `ssh`, e desligar
alguém às pressas — o caso em que a demora custa caro — passa a depender de
outra pessoa estar disponível.

A pergunta foi levada ao cliente e respondida: **o coordenador administra
usuários**.

## Decisão

Criar `/api/usuarios` com três rotas, todas do papel `coordenador`, e nenhuma
mais:

| rota | o que faz |
| --- | --- |
| `GET /api/usuarios` | lista, paginado, com filtro por `ativo` |
| `POST /api/usuarios` | cria e devolve, uma única vez, o token de definição de senha |
| `PATCH /api/usuarios/{id}` | altera nome, papel e `ativo` |

Quatro decisões acompanham a rota, e cada uma existe por causa de um ataque
concreto.

### 1. O coordenador não cria nem promove a `gestor`

Os papéis atribuíveis por esta API são `conferente` e `coordenador`. Tentar
`gestor` responde **403**, dizendo que ele é criado por linha de comando.

A razão está na forma da matriz do ADR 0001: `conferente` está contido em
`coordenador`, mas **`gestor` não é superconjunto de ninguém — é outro eixo**.
Ele lê a operação inteira, não a executa, e é o único que escreve baseline, que é
dado de gestão e não de conferência.

Logo, um coordenador que criasse um gestor estaria **se dando acesso a dado que o
papel dele não tem**: bastaria criar a conta e entrar nela. É escalada de
privilégio, ainda que por um caminho indireto — e a recusa vale também no
`PATCH`, senão a mesma escalada aconteceria em dois passos.

Criar gestor continua sendo `python -m homecareos.auth.cli criar`, que exige
acesso ao servidor. É uma operação rara (a matriz tem um punhado de gestores, não
um por turno) e o custo de exigir `ssh` para ela é baixo perto do de deixá-la
alcançável por uma sessão de coordenador comprometida.

### 2. A senha nunca passa por quem administra

A criação grava o hash de um valor **aleatório e descartado na mesma expressão** —
uma senha que ninguém conhece, nem quem criou a conta — e emite um token de
recuperação (`auth/recuperacao.emitir_token`), devolvido **uma única vez** na
resposta. Quem administra repassa o link à pessoa pelo canal que já usa, e ela
define a própria senha em `/redefinir-senha?token=…`.

A alternativa — uma senha temporária no corpo da requisição — foi descartada por
dois motivos. Quem administra passaria a conhecer a credencial de quem cadastrou,
e uma senha escolhida pelo administrador tende a virar um padrão (`Mudar@123`)
reusado na operação inteira. O fluxo de redefinição já existe, tem uso único e
expiração, e está testado (issue #34).

`senha_hash` é `NOT NULL`, e torná-la anulável seria migration. Mas mesmo com a
coluna anulável a escolha seria a mesma: senha ausente é um estado a mais para
todo caminho de login tratar, enquanto uma senha que ninguém conhece não abre
nada por construção.

**A conta e o token entram no mesmo commit.** `emitir_token` devolve `None`
quando o teto de emissões por hora foi atingido; para uma conta recém-criada isso
só acontece com `SENHA_RESET_MAX_POR_HORA <= 0`, mas o caso é tratado com
`rollback` e **503**, e não em silêncio: sem o token a conta nasceria sem nenhum
caminho de primeiro acesso, e ninguém perceberia até a pessoa reclamar.

### 3. Ninguém se tranca fora, e ninguém se promove

Três travas, com mensagens distintas:

- **não alterar o próprio papel** — é o único papel cuja alteração interessa a
  quem ataca (mudar o papel de outra pessoa não dá acesso a nada a quem chama), e
  proibi-lo é o que mantém a trava (1) valendo mesmo que a lista de papéis
  atribuíveis mude um dia;
- **não desativar a própria conta**;
- **não desativar nem rebaixar o último coordenador ativo** — sem coordenador
  ativo não sobra quem administre usuário nem quem edite regra, e a saída seria
  acesso ao banco ou ao servidor.

A terceira cobre o rebaixamento além da desativação, ainda que a issue nomeie só
a segunda: as duas esvaziam a coordenação do mesmo jeito, e travar só uma
deixaria a porta ao lado aberta. Com sessão de usuário ela é defesa em
profundidade (quem chama é sempre um coordenador ativo, e não pode agir sobre a
própria conta, então sempre resta ele); para a `X-API-Key`, que passa por
`exigir_papel` e não tem "si mesmo", ela é a única trava — e é por isso que a
verificação é feita contra o banco, e não contra o principal da requisição.

### 4. Desativar, nunca excluir — e desativar revoga sessão

Não há `DELETE`, e não é omissão: `log_conferencia.usuario_id` referencia
`usuarios`, e apagar uma pessoa apagaria a resposta a "quem fez esta ação?", que
é a razão de existir da issue #30.

Desativar **revoga todas as sessões abertas** da pessoa, na mesma transação.
`usuarios.ativo = false` sozinho já derruba o acesso na requisição seguinte
(`sessoes.resolver_sessao` recusa usuário inativo), mas sem a revogação uma
reativação futura ressuscitaria os cookies antigos — inclusive o de um aparelho
que a pessoa não tem mais.

## Consequências

**O que melhora.** Entrada e saída de gente deixam de exigir acesso ao servidor.
Desligar alguém passa a ser uma requisição, e essa requisição fecha as sessões
abertas junto — que é o comportamento que se espera de "desligar alguém", e que
o caminho pelo banco não dava de graça.

**O que custa.** A API passa a ter um endpoint cuja falha de autorização é
catastrófica: quem cria usuário decide quem entra. A mitigação está nas travas
acima e nos testes de `tests/test_api_usuarios.py`, que existem uma por trava.
E a operação passa a depender de haver sempre um coordenador ativo — o sistema
recusa a alteração que deixaria zero, mas nada obriga a existir dois, e uma
operação com um único coordenador não consegue desligá-lo pela API.

**O que fica em aberto.**

- **Não há auditoria de administração de usuário.** `log_conferencia` é ligada a
  documento (`documento_id` é `NOT NULL`) e não serve para registrar "quem
  promoveu fulano a coordenador". Uma tabela de auditoria administrativa é
  migration, e fica para a sua própria issue.
  **Resolvido pela issue #30**: a tabela `auditoria_usuarios` registra criação,
  alteração, desativação e reativação — ator, alvo, campo alterado (valor
  anterior e novo) e quando — na mesma transação da mutação, lida por
  `GET /api/usuarios/auditoria` (só coordenador). Não abriu ADR próprio: não
  muda quem administra usuário nem a matriz de papéis, só fecha a lacuna que
  este ADR já registrava como decisão pendente, sem introduzir direção
  arquitetural nova. Ver "Auditoria administrativa" no `apps/api/README.md`.
- **O token de definição de senha expira em `SENHA_RESET_VALIDADE_MINUTOS` (30).**
  Se o administrador demorar a repassá-lo, a pessoa precisa pedir um link em
  `POST /api/auth/senha/esqueci` — que depende de SMTP configurado. Sem SMTP, o
  caminho volta a ser o CLI.
- **`pendencia_responsavel_padrao` e o estreitamento da `X-API-Key`** continuam
  fora: a chave segue passando por qualquer checagem de papel, inclusive nestas
  rotas, e mudar isso é outra decisão com outro ADR (ADR 0001, item 3).

## Alternativas consideradas

**Deixar tudo no CLI.** É o estado anterior, e o mais seguro possível: administrar
usuário exige acesso ao servidor. Descartada porque transforma cada admissão e
cada desligamento num chamado para quem tem `ssh`, e o caso urgente — desligar
alguém agora — é justamente o que não pode depender de outra pessoa estar
disponível.

**Um papel `admin` novo, só para isto.** Separaria administrar usuário de
coordenar a conferência, e é o desenho mais limpo em tese. Descartada por ora: o
cliente respondeu que quem administra é o coordenador, e criar um quarto papel
sem ninguém para exercê-lo acrescentaria uma linha na matriz que ninguém pediu.
Se um dia a operação crescer a ponto de separar as duas coisas, a mudança é
trocar o papel exigido no `include_router` — não redesenhar a rota.

**Senha temporária escolhida por quem administra.** Descartada pelo item 2: faria
o administrador conhecer a credencial dos outros e viraria um `Mudar@123` reusado
na operação inteira.

**Deixar `gestor` atribuível pelo coordenador.** Seria uma linha a menos e a
matriz "parece" hierárquica. Descartada pelo item 1: `gestor` é outro eixo, e a
promoção seria escalada de privilégio disfarçada de conveniência.
