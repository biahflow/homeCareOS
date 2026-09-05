# ADR 0008 — Cifra do segredo TOTP em repouso

- **Status:** aceito
- **Data:** 2026-09-05
- **Origem:** a limitação declarada da issue #35 (segundo fator por TOTP) — "o segredo TOTP
  fica em claro no banco" —, registrada no `apps/api/README.md`, na docstring de
  `db/models/usuario.py` e na migration `e1f4a7c92b58`
- **Relacionado:** [ADR 0001](0001-autenticacao-de-usuario-na-api.md), que estabeleceu que
  credencial não fica legível no banco (senha em Argon2id, token de sessão em SHA-256);
  [ADR 0005](0005-rate-limit-das-rotas-caras.md), cujo registro de que a topologia de
  produção não está documentada também vale aqui
- **Numeração:** este ADR é o `0008` porque o `0007` está reservado a uma entrega em curso
  na mesma janela; não há relação entre os dois

## Contexto

O segundo fator por TOTP (issue #35) guarda o segredo de cada conta em
`usuarios.mfa_secret`, **em claro**. Quem tiver um dump do banco deriva o código de seis
dígitos de qualquer pessoa a qualquer momento, sem tocar no celular dela e sem deixar
rastro: o TOTP é uma função determinística do segredo e do relógio.

Isso destoa do resto da tabela. `senha_hash` é Argon2id, `sessoes.token_hash` é SHA-256, e
`codigos_recuperacao_mfa.codigo_hash` é Argon2id — as três decisões existem por um mesmo
princípio, escrito no ADR 0001: **um dump vazado não pode entregar credencial utilizável.**
`mfa_secret` era a única credencial da tabela que quebrava esse princípio.

A diferença não é descuido: hash não serve aqui. Senha e código de recuperação são
*verificados* (a aplicação compara o que chegou com o que está guardado), e por isso hash
basta. O segredo TOTP é *usado* — a aplicação precisa dele em claro para derivar o código
do passo atual —, e função de mão única não devolve nada. O que resolve um valor que
precisa voltar é cifra reversível, não hash.

### A justificativa antiga era falsa, e é o que muda

Até aqui, três lugares do repositório diziam a mesma coisa com as mesmas palavras:

> Não há KMS neste projeto, e "criptografar" com uma chave guardada no mesmo `.env` que
> acompanha o dump seria teatro: quem tem o banco geralmente tem a configuração.

A premissa **"a chave acompanha o dump"** é uma escolha de provisionamento, não um fato.
Um `pg_dump`, uma réplica de leitura, um backup em bucket e o resultado de uma injection
carregam o **conteúdo do banco** — não carregam variável de ambiente do processo da API.
A chave só acompanha o dump se alguém a puser lá.

O argumento estava certo sobre uma coisa: **cifrar não substitui KMS.** Estava errado ao
concluir que, por não ser cofre, não valia nada. As duas afirmações são independentes, e
tratá-las como uma só é o que manteve a coluna em claro por três entregas.

## Decisão

**Cifrar `usuarios.mfa_secret` em repouso com `Fernet`/`MultiFernet`, na fronteira do
banco, com a chave provisionada em `MFA_SECRET_KEYS` — separada do `DATABASE_URL`.**

### Onde a cifra mora: no tipo da coluna

A cifra é um `TypeDecorator` do SQLAlchemy (`db/cifra.SegredoCifrado`), e não chamadas de
`encrypt`/`decrypt` espalhadas por `auth/router.py`.

A razão é a superfície de erro. Os pontos de uso de `mfa_secret` no router tratam o campo
como presença (`is None` / `is not None`), atribuição (`= segredo`, `= None`) e leitura
(passada a `mfa.verificar_codigo`). Com o decorator, **nenhuma dessas linhas mudou** — e,
mais importante que a economia de diff, não sobrou caminho para uma escrita futura
esquecer de cifrar. Quem grava na coluna passa pelo tipo, sempre. É a mesma forma de
raciocínio que pôs a autorização por papel em `main.py`, por router, em vez de endpoint a
endpoint: um endpoint novo nasce protegido por construção.

O segredo continua base32 em memória e na resposta de `POST /api/auth/mfa/iniciar` — é o
que a pessoa cadastra no app autenticador, e é dele que `otpauth_uri` deriva o QR code. A
cifra é **em repouso**; o trânsito já é TLS mais sessão.

### `MultiFernet`, e não `Fernet` simples

`MFA_SECRET_KEYS` é uma lista separada por vírgula, com a mesma semântica de `API_KEYS`:
**a primeira cifra, todas decifram.**

Com `Fernet` simples, trocar a chave exigiria parar a aplicação, rodar um script que
decifra tudo com a chave velha e recifra com a nova, e só então subir com a configuração
trocada. Uma rotação que precisa de downtime e de um script de emergência é uma rotação
que não acontece — e chave que nunca roda é chave que fica anos no mesmo lugar,
acumulando cópias em backups, laptops e histórico de terminal.

Com a lista, rotacionar é acrescentar a chave nova na frente e deixar a antiga até nenhum
segredo depender mais dela. Sem parar nada.

### Sem chave, o MFA recusa operar

`POST /api/auth/mfa/iniciar` responde **503** quando `MFA_SECRET_KEYS` está vazio, antes
de gerar segredo, e nada é gravado. A escrita na coluna também levanta, como defesa em
profundidade.

503 e não 500: é indisponibilidade de configuração do servidor, não erro de quem chama — a
mesma requisição passará a funcionar assim que a chave existir, sem ninguém mudar o
cliente.

O que **não** acontece é gravar em claro. Um sistema que degrada em silêncio para texto
claro é pior que um que recusa: quem ativou o segundo fator achando que estava protegido
não tem como descobrir que não estava.

### A aplicação sobe sem a chave, com aviso

Diferente de `API_KEYS`, cuja ausência fora de `local` **impede o boot** (ADR 0001).

A diferença é o alcance da falta. Sem `API_KEYS`, toda rota de `/api/*` perde uma
credencial. Sem `MFA_SECRET_KEYS`, o que para é um recurso **opcional por pessoa**: quem
não ativou o segundo fator não percebe nada, e quem já o ativou continua logando enquanto
a chave que cifrou o segredo dele estiver na lista. Derrubar a API inteira por causa disso
trocaria uma indisponibilidade parcial por uma total.

Chave **presente e malformada** é o caso oposto e recusa subir. Quem escreveu a variável
quis cifrar; tratar o typo como ausência desligaria a cifra justamente para quem pediu por
ela, e o sintoma apareceria só na primeira pessoa que tentasse ativar o MFA. É o mesmo
raciocínio que a migration `a4d6c8b21f37` aplica a um canal desconhecido em
`ALERTAS_CANAIS`.

## O que isto fecha, e o que não fecha

Esta parte é o ADR. O resto é implementação.

**Fecha o vetor comum** — aquele que de fato acontece:

| vetor | por que a cifra ajuda |
| --- | --- |
| backup vazado (bucket aberto, fita, cópia em laptop) | o backup carrega o banco, não a variável de ambiente da API |
| réplica de leitura | replica dado, não configuração de processo |
| acesso de DBA / consulta ad hoc em produção | quem administra o banco não precisa da chave da aplicação para o trabalho dele |
| dump obtido por SQL injection | a injection lê tabela; não lê o ambiente do processo |

Nesses quatro casos o atacante tem o **conteúdo** do banco e não tem `MFA_SECRET_KEYS`. O
segredo é um token opaco, e o segundo fator continua valendo.

**Não fecha host inteiro comprometido.** Quem executa código no servidor da API lê a
variável de ambiente e o banco juntos. Nesse cenário a cifra não atrapalha o atacante em
nada — e ele já tem à mão coisas piores que gerar TOTP.

Também não fecha: acesso legítimo à aplicação em execução, malware no dispositivo da
pessoa, engenharia social. Nenhum deles passa pelo banco.

**Portanto: é redução real de superfície, não é cofre.** A frase que este ADR substitui
errava por tratar "não é cofre" como sinônimo de "não vale nada". As duas metades precisam
ficar escritas juntas, porque separadas cada uma vira propaganda: sozinha, a primeira
promete um cofre que não existe; sozinha, a segunda justifica não fazer nada.

## Consequências

### A chave vira material de backup tão crítico quanto o banco

E precisa ser guardada **em outro lugar que ele** — guardada junto, ela não protege de
nada, e a premissa falsa do texto antigo passaria a ser verdadeira por descuido de
operação.

Perder `MFA_SECRET_KEYS` torna ilegível o segundo fator de todas as contas que o têm
ativo. Isso é uma consequência nova e real: antes, o pior caso de um banco restaurado era
"o segredo vazou"; agora existe também "o segredo não abre".

### A saída de quem fica com o segredo ilegível é o código de recuperação

E ela foi verificada, não presumida. `codigos_recuperacao_mfa` guarda hash Argon2id de
códigos **independentes do segredo TOTP**, e o caminho de volta continua funcionando
porque a leitura de um segredo indecifrável **degrada para `None` em vez de levantar**:

```text
mfa_secret ilegível → process_result_value devolve None
                    → /mfa/verificar pula o TOTP (só o tenta quando há segredo)
                    → cai no código de recuperação
                    → a pessoa entra
```

Levantar na leitura derrubaria com 500 justamente a rota de emergência, e a conta ficaria
sem saída nenhuma. É a decisão menos óbvia deste ADR, e a que mais importa no pior dia.

**O que essa degradação custa, declarado:** com o segredo ilegível, `/mfa/desativar` e
`/mfa/reemitir-codigos` respondem **409 "o segundo fator não está ativado nesta conta"** —
elas exigem `mfa_secret is not None`. A pessoa entra pelo código de recuperação e não
consegue desligar o próprio MFA pela API; sair desse estado exige quem administre o banco.
É aceitável porque o cenário é "a chave foi perdida", que já é operação de desastre, e
porque a alternativa (deixar a rota aceitar segredo ilegível) exigiria distinguir "sem MFA"
de "MFA ilegível" em todo o fluxo. Fica registrado como trabalho futuro, não como
comportamento desejado.

**Resolvido pela issue #39, para `/mfa/desativar`.** A distinção temida acabou custando
uma linha: `mfa_ativado=True` com a coluna vazia **é** "MFA ilegível", porque os dois
campos são limpos no mesmo commit da desativação e `/mfa/confirmar` só liga a flag com
segredo gravado — a flag ligada com coluna vazia não é estado alcançável pelo fluxo
normal. Nesse estado a rota aceita **senha + código de recuperação** no lugar de senha +
TOTP, e o 409 passou a significar só `mfa_ativado=False`. Não há degradação de segurança:
continuam sendo dois fatores, e o código de recuperação já é a credencial que pula o
segundo fator no login — quem tem senha e código de recuperação já entra na conta. A
ordem de verificação importa e está no código: a senha é conferida **primeiro**, porque
consumir o código de recuperação antes dela faria um erro de digitação queimar um item de
uma lista finita. **`/mfa/reemitir-codigos` continua respondendo 409**, por decisão e não
por esquecimento: com o segredo ilegível o segundo fator está quebrado, e emitir oito
códigos novos não devolve o app autenticador a ninguém — o caminho é desligar e religar
com `/mfa/iniciar`.

### A migration de dados é obrigatória, e falha alto sem chave

`f2b9d6e04a17` reescreve os segredos existentes. Com linhas para converter e sem chave,
ela **para** com mensagem: pular em silêncio deixaria a coluna metade cifrada e metade em
claro, sem ninguém saber quais, e cada pessoa descobriria pelo login que parou de
funcionar. Sem nenhum segredo no banco, roda sem chave — é o caso do CI e de todo ambiente
que ainda não usa o segundo fator.

O `downgrade()` **decifra de volta**, e isso não é simetria decorativa: um rollback que
deixasse o token Fernet na coluna devolveria o banco ao esquema antigo com conteúdo
ilegível, o código anterior o trataria como segredo base32 válido, e nenhum código TOTP
jamais bateria — para todo mundo, em silêncio, sem erro em log nenhum. O rollback ficaria
pior que o problema que ele desfaz.

### O que não muda

- **A coluna continua `String`.** O token Fernet é base64url e cabe onde já estava; trocar
  para `LargeBinary` reescreveria a tabela sem ganho.
- **`gerar_segredo()` continua base32.** `uri_otpauth` e todo app autenticador dependem do
  formato.
- **Nenhum ponto de uso em `auth/router.py` mudou** por causa da cifra. A única linha nova
  ali é a guarda do 503, que é comportamento novo — não adaptação.
- **Nenhuma consulta quebra.** A cifra não é determinística (IV e timestamp novos a cada
  `encrypt`), e o projeto não compara `mfa_secret` por igualdade em lugar nenhum: só
  `IS NULL` / `IS NOT NULL`.

### Custo operacional

- Uma dependência nova de verdade (`cryptography`), o que obriga
  `docker compose --profile tools build` antes do próximo `up`.
- Uma variável a provisionar em cada ambiente, com backup próprio e política de rotação.
- Um `Fernet.encrypt`/`decrypt` por leitura ou escrita da coluna — irrelevante ao lado dos
  Argon2 que os mesmos fluxos já pagam.

## Alternativas consideradas

**Não fazer nada, mantendo a limitação declarada.** É o estado anterior, e tinha a virtude
de ser honesto. Deixa de se defender quando a premissa que o sustentava (a chave acompanha
o dump) se revela uma escolha, e não um fato.

**KMS/HSM (AWS KMS, GCP KMS, Vault).** É a resposta tecnicamente certa: a chave nunca
existe em claro no processo, o uso é auditado, e a revogação é central. Descartada **por
ora, e não por mérito** — traz dependência de infraestrutura para operar, monitorar e
custear, exige credencial de nuvem no ambiente local e no CI, e o repositório sequer
documenta a topologia de produção (o ADR 0005 já registra essa lacuna). O ganho sobre a
decisão deste ADR é justamente o cenário de host comprometido, que é o mais raro dos
vetores desta tabela.

**Este ADR é o passo intermediário, não o final.** Quando houver KMS, o caminho é envelope
encryption: a chave de dados continua cifrando a coluna, e o KMS cifra a chave de dados.
`MFA_SECRET_KEYS` vira o material que o KMS entrega, e `db/cifra.py` é o único lugar a
mudar — foi por isso que a cifra ficou concentrada no tipo da coluna. **Quando isso
acontecer, este ADR deve ser substituído, não remendado.**

**Cifra de coluna no Postgres (`pgcrypto`).** Mantém a cifra perto do dado e dispensa
código de aplicação. Descartada porque a chave passaria pela conexão em cada consulta,
apareceria em `pg_stat_activity` e nos logs de statement do banco, e ficaria acessível a
exatamente quem a cifra deveria excluir: quem tem acesso ao banco. Cifrar dentro do
processo que se quer proteger contra o banco é resolver o problema errado.

**Transparent Data Encryption / cifra de disco.** Protege contra roubo do disco físico e
nada mais: um `pg_dump` legítimo, uma réplica e uma injection continuam devolvendo texto
claro, porque para o banco em execução o dado está descifrado. São exatamente os quatro
vetores que este ADR quer fechar.

**`Fernet` simples, com uma chave só.** Menos configuração e menos código. Descartada pelo
que ela faz com a rotação, descrito acima: uma troca de chave passaria a exigir downtime e
um script de emergência, e o desfecho previsível é a chave nunca rodar.

**AES-GCM direto, com nonce próprio.** Mais rápido e sem o timestamp que o Fernet carrega.
Descartada porque exige que este projeto decida modo, nonce e formato de serialização — e
essas são as três escolhas em que implementações de cifra costumam errar. `Fernet` é uma
construção fechada, sem parâmetro para acertar errado.

**Hash, como `senha_hash` e os códigos de recuperação.** Não é alternativa: o segredo TOTP
precisa voltar em claro para derivar o código do passo. Fica registrado porque é a primeira
pergunta de quem lê a tabela e vê três colunas hasheadas e uma cifrada.

## O que fica em aberto

- **A rotação não reescreve dado antigo.** Uma chave só sai da lista com segurança depois
  que nenhum segredo depender dela, e hoje nada mede isso nem recifra as linhas em
  segundo plano. Enquanto isso, a chave antiga fica na lista — o que é seguro, mas adia a
  aposentadoria dela indefinidamente. Um comando de recifra pertence a uma issue própria.
- **`/mfa/desativar` responde 409 para segredo ilegível**, como descrito nas
  consequências. Distinguir "sem MFA" de "MFA ilegível" no fluxo de gestão é trabalho
  futuro.
  **Resolvido pela issue #39**: a rota aceita senha + código de recuperação enquanto o
  segredo estiver ilegível, e o 409 ficou reservado a `mfa_ativado=False` (ver as
  consequências). **O que sobra em aberto é o caso de borda**: quem perder a chave **e**
  esgotar os códigos de recuperação continua trancado — não há rota administrativa que
  desative o MFA de terceiro (`auth/usuarios_router.py` não expõe nada de MFA), e a saída
  é intervenção direta no banco. Criar essa rota é decisão de outra natureza — quem
  administra usuário passaria a poder desligar o segundo fator de outra pessoa — e
  pertence à sua própria issue, com o ADR 0004 na mesa.
- **Não há rotina de verificação da chave.** Ninguém confere periodicamente que
  `MFA_SECRET_KEYS` ainda abre o que está no banco; o primeiro sinal de chave errada é o
  `logger.error` de `db/cifra.py`, que só sai quando alguém tenta logar.
- **A política de backup da chave não está escrita em lugar nenhum**, porque este
  repositório não descreve a operação de produção. Enquanto isso não existir, a instrução
  vive no `.env.example` e neste ADR.
