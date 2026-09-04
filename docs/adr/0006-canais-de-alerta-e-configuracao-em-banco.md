# ADR 0006 — Canais de alerta e configuração em banco

- **Status:** aceito
- **Data:** 2026-09-04
- **Issue:** #9
- **Relacionado:** [ADR 0001](0001-autenticacao-de-usuario-na-api.md), que criou a matriz
  de papéis e o modelo de usuário de que o destinatário por papel depende;
  [ADR 0004](0004-administracao-de-usuarios-pela-api.md), cuja leitura de "o que cada
  papel pode" este ADR precisa esticar

## Contexto

Os alertas da issue #9 saem por um canal só: WhatsApp, pela uazapi. O gateway foi
exercitado contra o serviço real pela primeira vez em 04/09/2026 e funciona — mensagem
entregue, cooldown suprimindo a segunda varredura, token sem vazar para banco ou log.

O pedido é que **cada canal seja ligável e desligável separadamente**, com e-mail como
segundo canal, e que quem decide isso o faça por uma tela, não editando `.env` no
servidor.

Quatro fatos do levantamento mudam o desenho, e o quarto contradiz uma premissa do
pedido.

### 1. Não existe canal de e-mail para alertas, e a porta de e-mail que existe é estreita

`mailer/` serve a um caso só: o link de recuperação de senha. As duas portas divergem na
assinatura — `WhatsAppProvider.enviar(destinatario, mensagem)` contra
`EmailProvider.enviar(destinatario, assunto, corpo)` — e **nenhuma das duas trata HTML**:
`SmtpEmailProvider` usa `set_content(corpo)`, texto puro, decisão deliberada e
documentada para o caso do link.

Consequência direta: os templates de alerta usam marcação de WhatsApp (`*negrito*`) e
emoji. Mandados por e-mail hoje, os asteriscos apareceriam **literais**. E não há campo de
assunto em template de alerta nenhum — `renderizar()` devolve uma string só.

### 2. Sem uma coluna de canal, o anti-bombardeio quebra em silêncio

`alertas_enviados.destinatario` é hoje uma string que acumula dois papéis: identifica o
**canal** (é um telefone) e identifica a **pessoa**. As duas defesas contam sobre ela:
cooldown por `(tipo, chave, destinatario)`, rate limit por `destinatario`.

Se a mesma pessoa passar a receber por telefone e por e-mail, as duas linhas viram
destinatários **não relacionados**. O efeito não é um erro: é o teto de mensagens por hora
**dobrar sem ninguém pedir**, e o mesmo aviso chegar duas vezes para a mesma pessoa. O
rate limit existe justamente para proteger a pessoa, não o endereço — e hoje a pessoa não
é modelada nessa tabela (não há FK para `usuarios`).

### 3. Não há telefone no cadastro de usuário

`Usuario` tem `email`, `papel` e `ativo`; **não tem telefone**. Então "destinatário
resolvido por papel" funciona para e-mail e **não funciona** para WhatsApp, que continua
dependendo da lista de telefones do `.env`. Qualquer desenho que finja simetria entre os
dois canais está mentindo sobre o que o sistema sabe.

### 4. Configuração de sistema em banco não tem precedente — e o papel pedido contraria a matriz

Procurei em todos os models e migrations: **não existe tabela de configuração,
feature flag ou parâmetro de sistema editável**. O que mais se aproxima:

- `/api/regras` — configuração de domínio editável por API, **exclusiva do coordenador**;
- `Operadora.dia_envio` — persistido em banco e ajustado **por SQL direto**, sem tela;
- `Operadora.config` — JSONB sem nenhum endpoint de escrita.

E o papel. A matriz do ADR 0001, reafirmada pelo ADR 0004, diz que **`gestor` não é
superconjunto de ninguém: é outro eixo — lê a operação inteira, não a executa**. Hoje o
gestor tem **um único write** em todo o sistema: `PUT /api/relatorios/baseline`. Tudo o
que é configuração — regras, usuários — é do coordenador, e o gestor recebe 403.

Uma primeira versão deste ADR pôs a configuração dos canais no gestor, e isso lhe daria o
segundo write do sistema. Ficou registrado aqui porque a alternativa foi considerada e
descartada por incoerência com a matriz: ligar e desligar canal é operação, e quem opera
é o coordenador.

Há um agravante de segurança: **quem desliga um canal silencia a operação.** Se o
WhatsApp for desligado por engano numa sexta-feira, ninguém recebe o aviso de prazo de
competência e ninguém percebe — a ausência de alerta é indistinguível de "não havia o que
alertar", que é exatamente o que a tela de alertas da issue #39 já avisa sobre o cooldown.

## Decisão

### Canal vira um conceito de primeira classe

Uma porta comum de canal, com a operação de despachar um alerta já renderizado para um
destinatário. As duas implementações — WhatsApp sobre a uazapi, e-mail sobre o `mailer` —
ficam atrás dela, e `alerts/service._despachar_para` deixa de conhecer `WhatsAppProvider`
diretamente.

O template passa a ser **por canal**: o de WhatsApp mantém emoji e `*negrito*`; o de
e-mail nasce em texto puro, com assunto próprio. Não se resolve isso "tirando o negrito
dos dois": a marcação existe porque no WhatsApp ela funciona, e apagá-la piora o canal que
hoje é o único que roda.

### O log ganha uma coluna de canal, e o anti-bombardeio passa a contar por pessoa

`alertas_enviados` ganha `canal`. E as duas defesas mudam de chave: o cooldown continua
por assunto e destinatário — dois canais são dois endereços, e faz sentido o mesmo aviso
sair nos dois —, mas o **rate limit passa a contar por pessoa quando a pessoa é
conhecida**, não por endereço. Sem isso, ligar o segundo canal dobra o teto que existe
para proteger quem recebe.

Quando a pessoa não é conhecida (telefone avulso do `.env`), o endereço continua sendo a
chave — é o melhor que o dado permite, e a assimetria fica declarada em vez de escondida.

### A configuração dos canais vive em banco, numa tabela própria — não numa tabela de settings genérica

Uma tabela genérica de chave-valor de configuração é o caminho que parece barato e cobra
depois: perde tipo, perde validação, perde migration, e vira o lugar onde qualquer coisa é
enfiada sem revisão. A tabela é dos **canais de alerta**, com uma linha por canal e o
estado dele.

Configuração em banco não substitui a credencial no `.env`. São duas perguntas
diferentes, e as duas precisam de resposta afirmativa para um canal enviar:

```
canal habilitado (banco)  ×  credencial presente (.env)  =  canal envia
```

Isso resolve a ambiguidade que existe hoje, em que "desligado porque decidi" e "desligado
porque não configurei" são indistinguíveis. A tela precisa mostrar os dois estados
separadamente, sob pena de alguém ligar um canal na interface e não entender por que nada
sai.

### Quem configura: o coordenador

Ligar e desligar canal é do **coordenador**, e o gestor lê — mesma divisão que
`/api/alertas` já tem hoje.

Isso mantém a matriz do ADR 0001 intacta, e pela razão certa: quem configura o sistema é
quem o opera. As regras de validação por operadora e o cadastro de quem entra já são do
coordenador; por onde a operação é avisada é da mesma natureza. O gestor continua com um
write só, o baseline, que é dado de gestão e não de operação.

**Quem desliga um canal silencia a operação**, e é isso que torna a auditoria obrigatória
— não completude: **a mudança de estado de um canal é auditada**. Quem
ligou, quem desligou, quando. Sem isso, "por que ninguém foi avisado?" é uma pergunta sem
resposta possível — e é a pergunta que vai ser feita.

A auditoria administrativa que existe (`auditoria_usuarios`) **não serve**: o schema exige
`alvo_usuario_id` não-nulo com FK para `usuarios`, e `mudancas` é construída comparando
campos de `Usuario`. Registrar "fulano desligou o WhatsApp" ali obrigaria a inventar um
alvo fictício. O padrão do projeto é uma tabela de auditoria **por entidade de domínio**
(`log_conferencia` para documento, `auditoria_usuarios` para usuário), e a mudança de
canal segue a mesma regra.

### Destinatários: por papel no e-mail, por lista no WhatsApp

O e-mail resolve destinatário pelo **papel** — os e-mails das contas ativas com o papel
alvo. Isso fecha uma limitação que a issue #30 registrou: hoje os destinatários são
telefones soltos no `.env`, sem vínculo com pessoa nenhuma, e quem sai da equipe continua
recebendo até alguém lembrar de editar a variável.

O WhatsApp **continua com a lista do `.env`**, porque não há telefone no cadastro. A
assimetria é consequência do dado que existe, não escolha de desenho, e some no dia em que
`Usuario` tiver telefone — o que é outra decisão, com o seu próprio custo de LGPD.

## Consequências

- Ligar o e-mail sem SMTP configurado não envia nada, e a tela precisa dizer isso — é o
  mesmo modo de falha que a recuperação de senha já tem, e lá a única pista hoje é uma
  linha de log.
- **O mesmo alerta chega duas vezes para quem estiver nos dois canais**, e isso é o
  comportamento desejado: são canais distintos, não fallback. Um canal como reserva do
  outro é outra feature, e exigiria saber que o primeiro falhou.
- A varredura passa a fazer mais trabalho por alerta (resolver destinatário por papel é
  uma consulta a `usuarios`), e ela roda de minuto em minuto pelo cron.
- Um canal desligado não some do log: o que já foi enviado continua lá, e a tela de
  alertas continua sendo a fonte de verdade do que saiu.
- Desligar um canal pela tela é reversível em um clique; desligar por engano só é
  perceptível quando alguém repara que parou de receber. A auditoria é o que torna isso
  investigável depois.

## Alternativas consideradas

**Flags no `.env`, sem tela.** É o caminho barato, simétrico com todo o resto do projeto e
sem tabela nova, sem migration, sem endpoint. Descartado porque o pedido é explícito:
quem decide não deve depender de acesso ao servidor. Vale registrar que este ADR troca
simplicidade por autonomia de quem opera — e que a troca só se paga se a tela existir de
verdade; um endpoint sem tela seria o pior dos dois mundos.

**Tabela genérica de configuração (chave-valor).** Serviria a este caso e aos próximos.
Descartada por experiência conhecida: sem tipo e sem validação, ela vira o depósito onde
configuração entra sem revisão, e a primeira migration que precisar mudar o formato de um
valor não tem onde se apoiar. Quando houver um terceiro caso de configuração em banco,
vale reconsiderar — com ADR próprio.

**Reaproveitar `auditoria_usuarios` para a mudança de canal.** Descartada pela forma do
schema (ver acima). Forçar um alvo fictício numa tabela cuja razão de existir é responder
"quem fez o quê **em quem**" corromperia justamente o dado que a issue #30 criou.

**Canal de e-mail como fallback do WhatsApp.** Mais inteligente na aparência: manda por
e-mail só quando o WhatsApp falha. Descartada por ora — exige saber que o envio falhou de
verdade (o gateway aceitar a mensagem não prova entrega ao celular) e transforma um
desenho de dois canais independentes numa máquina de estados com retry. Se for desejado,
merece ADR próprio.

**Não fazer.** O WhatsApp funciona e acabou de ser provado. O argumento contra é
dependência de canal único: um gateway com problema, um número trocado ou um telefone
perdido silenciam a operação inteira, e hoje não há segundo caminho.

## O que fica em aberto

- **Os alertas por e-mail vão para quais papéis, em qual tipo de alerta?** Mandar todos os
  quatro tipos para coordenador e gestor é o padrão óbvio, e é provavelmente ruído demais.
  Isso é calibragem de produto e precisa de uma conversa, não de um default inventado aqui.
- **Não há telefone no cadastro de usuário**, e enquanto não houver, o WhatsApp não pode
  resolver destinatário por papel.
- **A entrega de WhatsApp não é confirmada**: a uazapi aceitar a mensagem não prova que
  ela chegou. Nada neste ADR muda isso.
