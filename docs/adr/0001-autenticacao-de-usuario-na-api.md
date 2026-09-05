# ADR 0001 — Autenticação de usuário na API

- **Status:** aceito
- **Data:** 2026-09-03
- **Issue:** #30 (descarta a #29)

## Contexto

A API autentica hoje por `X-API-Key`: uma lista de segredos compartilhados em
`API_KEYS`, comparada em tempo constante por `api/auth.py` e aplicada por
router em `main.py`. O desenho é correto para o que ele resolve — integração
máquina-a-máquina — e resolve bem: chave ausente e chave errada devolvem o
mesmo corpo, a chave nunca é logada, e a lista separada por vírgula permite
rotação sem downtime.

Ele não resolve duas coisas que agora bloqueiam.

**O frontend não consegue chamar a API.** O shell existe (`feat/f1-trilha-d`,
commit `c454398`), compila e roda, mas não tem noção de autenticação: não há
`X-API-Key` em `apps/web/` nem em `packages/contracts/`, e a página de login
apenas navega — o comentário dela registra que "não há backend de autenticação
ainda". Verificado contra a API em execução:

```
POST /api/documentos  sem header  -> 401 {"tipo":"unauthorized","mensagem":"credencial inválida"}
GET  /api/operadoras  com a chave -> 200
```

A saída aparentemente óbvia — guardar a chave no navegador — está descartada:
`API_KEYS` concede acesso total a `/api/*`, prontuário clínico incluso. No
browser, todo usuário fica com a chave-mestra, e revogar uma chave passa a
derrubar todo mundo.

**A auditoria é anônima.** O sistema registra ação sem saber de quem:

| lugar | valor hoje |
| --- | --- |
| `log_conferencia.usuario` | `"api"` (routers) ou `"sistema"` (dispatcher de extração) |
| `pendencias.responsavel` | `"equipe-conferencia"`, placeholder de `config.py` |

O próprio `config.py` registra a dívida: "não é um id de usuário porque não
existe modelo de usuário ainda". Numa conferência com várias pessoas,
"quem transicionou esta pendência?" é pergunta operacional corriqueira, e hoje
não tem resposta.

## Decisão

Introduzir autenticação de usuário na API, com sessão no servidor, mantendo
`require_api_key` para integração máquina-a-máquina.

**1. Identidade.** Tabela de usuários com credencial hasheada por **Argon2id**
(`argon2-cffi`). Nunca o `hmac.compare_digest` de `api/auth.py`: aquilo compara
segredo compartilhado de alta entropia, e senha escolhida por pessoa exige
função lenta e com sal.

**2. Sessão em cookie `httpOnly`, com estado no Postgres.** O cookie carrega um
identificador opaco; a sessão vive numa tabela e é consultada por requisição.

A alternativa stateless (JWT) foi descartada por causa da revogação: desligar o
acesso de alguém a prontuário clínico não pode esperar um token expirar. Uma
denylist devolveria o estado que o JWT prometia evitar, com dois mecanismos em
vez de um.

O custo da consulta por requisição foi avaliado e é aceitável: é acerto de
índice único em linha única, sobre requisições que já fazem várias consultas
(o relatório de conferência faz quatro por página). O gargalo do sistema é a
extração por Vision, que leva segundos por página. Se a medição um dia
contrariar isso, a saída é cache de sessão sem trocar o modelo.

**3. Coexistência com `X-API-Key`.** A chave continua válida e é o caminho da
integração sem humano. Sessão de usuário e chave de máquina são credenciais de
naturezas diferentes e não se substituem.

**Correção ([ADR 0007](0007-escopo-de-papel-da-chave-de-api.md)).** Este item
afirmava que o consumidor da chave era, "hoje", o cron da varredura de alertas
(`python -m homecareos.alerts.scan`). Era falso: o cron abre uma sessão do banco
e não faz requisição HTTP nenhuma. A afirmação foi repetida no README, no ADR
0005 e num teste, e sustentava a decisão de a chave passar por qualquer checagem
de papel. O ADR 0007 corrige a justificativa e estreita a chave — o que ela abre
passa a ser declarado em `API_KEY_PAPEIS`, com default restritivo.

**4. Três papéis:** `conferente`, `coordenador`, `gestor`.

A matriz abaixo é **proposta derivada dos endpoints existentes, não requisito
levantado com o cliente**, e precisa de confirmação antes de virar autorização
em código. Errar aqui custa migration de dado depois.

| capacidade | conferente | coordenador | gestor |
| --- | :-: | :-: | :-: |
| Enviar documento (`POST /api/documentos`) | ✅ | ✅ | — |
| Ler documentos e revalidar | ✅ | ✅ | ✅ |
| Transicionar pendência (`PATCH /api/pendencias/{id}`) | ✅ | ✅ | — |
| Relatório de conferência e CSV | ✅ | ✅ | ✅ |
| Editar regras (`/api/regras`) | — | ✅ | — |
| Disparar varredura e ler log de alertas | — | ✅ | ✅ |
| Métricas agregadas | — | ✅ | ✅ |
| Registrar baseline de glosa (`PUT /api/relatorios/baseline`) | — | — | ✅ |

`conferente` está contido em `coordenador`. `gestor` **não** é superconjunto de
ninguém: é outro eixo — lê a operação inteira, não a executa, e é o único que
escreve baseline, que é dado de gestão e não de conferência.

## Consequências

**O que melhora.** `log_conferencia.usuario` e `pendencias.responsavel` passam
a nomear pessoas, e a pergunta "quem fez isso?" ganha resposta. O frontend
destrava sem que a chave-mestra chegue ao navegador. E os destinatários de
alerta por papel que a issue #9 previu — hoje uma lista de telefones no `.env`,
sem vínculo com pessoa nenhuma — passam a ter a que se ligar.

**O que custa.** Dado sensível novo no banco (credenciais), com o cuidado de
LGPD que vem junto. `usuario` deixa de ser string literal e passa a vir do
request, o que toca `classification/service.py`, os quatro routers de `api/` e
o dispatcher de extração. E escopo que cresce sozinho: expiração de sessão,
recuperação de senha, bloqueio por tentativa.

**O que fica em aberto.** A matriz de papéis acima é proposta. Confirmar com o
cliente **antes** de codar autorização; o modelo de usuário e a sessão podem
ser construídos sem essa confirmação, a autorização não.

## Alternativas consideradas

**BFF no Next (issue #29, descartada).** As route handlers do Next guardariam a
chave no servidor e o navegador receberia um cookie. Resolveria o vazamento com
uma fração do custo e **sem tocar na API** — mas deixaria a auditoria anônima,
que é metade do problema. Continua sendo o paliativo válido se o prazo apertar,
e por isso a #29 foi fechada como `not planned` e não como concluída.

**Chave de API por usuário.** Uma entrada em `API_KEYS` por pessoa daria
identidade sem modelo de usuário. Descartada: `api_keys` é configuração de
ambiente, não cadastro — criar ou revogar pessoa exigiria deploy, e o segredo
ficaria no navegador do mesmo jeito.

**Provedor externo (OAuth/OIDC).** Tira senha do nosso banco e resolve
recuperação e MFA de graça. Descartada por ora: acrescenta dependência externa
e configuração de tenant a um sistema que ainda não tem usuário nenhum. Vale
reavaliar quando houver operação real — a decisão de sessão tomada aqui não
impede a migração.
