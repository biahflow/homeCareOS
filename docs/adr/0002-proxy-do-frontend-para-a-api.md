# ADR 0002 — Proxy do frontend para a API

- **Status:** aceito
- **Data:** 2026-09-03
- **Issue:** #39

## Contexto

A fundação do monorepo trouxe `apps/web/` (Next.js) e `packages/contracts/`
para a `main`. O scaffold herdado chama a API **direto do navegador**: o
`apps/web/lib/env.ts` exportava `API_BASE_URL` apontando para
`http://localhost:8001`, e a página de documentos passa esse valor para
`uploadDocumento` do pacote de contratos.

Isso é incompatível com a autenticação que a API já tem, por duas razões
verificadas no código, não supostas.

**A sessão é um cookie `httpOnly` com `SameSite=Lax`.** O
`_setar_cookie` em `apps/api/src/homecareos/auth/router.py:119` escreve o
cookie com `httponly=True`, `samesite="lax"`, `path="/"` e `secure`
condicional ao ambiente. O docstring registra explicitamente que o `lax` está
ali como defesa de CSRF: "o cookie não acompanha requisição cross-site
iniciada por outro domínio, que é o que o CSRF explora". Uma chamada de
`localhost:3000` para `localhost:8001` é cross-site para essa regra, e o
cookie não vai junto.

**A API não tem CORS.** `grep -rn -i cors apps/api/src/` não devolve nada. Sem
`Access-Control-Allow-Origin`, o navegador nem chega a entregar a resposta ao
JavaScript da página.

A combinação não produz um erro legível: não é um 401 que o front possa
tratar, é uma requisição que não completa. O front destravaria em
desenvolvimento e quebraria de formas diferentes em cada ambiente.

## Decisão

**O navegador fala apenas com a origem do Next.** Todo caminho `/api/*` é
repassado ao backend por `apps/web/proxy.ts` — o arquivo que até o Next 15 se
chamava `middleware.ts` e que a versão 16 renomeou para `proxy.ts` —, executado
pelo servidor do Next a cada requisição.

**Não** por `rewrites` no `next.config.ts`, e o motivo é medido, não estético:
o `next build` serializa a `destination` dos rewrites em
`.next/routes-manifest.json`. A URL da API fica congelada na imagem em build
time. Uma imagem construída com o default de desenvolvimento e promovida para
o Compose levava `http://localhost:8001` junto e respondia **500
(ECONNREFUSED)**, mesmo com `API_URL=http://api:8000` corretamente presente no
ambiente do container.

Isso foi verificado, não deduzido: com o build feito sob
`API_URL=http://localhost:9999` e o servidor iniciado sob
`API_URL=http://localhost:8001`, o manifest continha `9999` e a resposta veio
da API em `8001` (`server: uvicorn`). O `proxy.ts` lê `process.env` em runtime;
o rewrite de configuração, não.

Vale registrar que esse era o **mesmo defeito de classe** do
`NEXT_PUBLIC_API_URL` que esta decisão removeu — URL de ambiente congelada em
build time —, apenas deslocado da configuração do bundle para o manifesto de
rotas, onde é mais silencioso: não há `ARG` no Dockerfile que denuncie a
dependência.

Em consequência:

- `apps/web/lib/env.ts` passa a exportar `API_BASE_URL = ""`, de modo que o
  cliente monte caminho **relativo** (`/api/documentos`);
- a URL real da API vira `API_URL`, variável **de servidor**, lida em runtime
  pelo `proxy.ts` a cada requisição, com default `http://localhost:8001` (a
  porta que o Compose publica no host);
- no Compose, o serviço `web` recebe `API_URL: http://api:8000` — servidor
  para servidor, pela rede interna, sem depender da porta publicada.

`API_URL` **não** pode ganhar o prefixo `NEXT_PUBLIC_`. O Next inlina tudo que
é `NEXT_PUBLIC_*` no bundle entregue ao navegador, e o ponto inteiro desta
decisão é que o navegador não conheça a API. Pelo mesmo motivo o
`ARG/ENV NEXT_PUBLIC_API_URL` saiu do `apps/web/Dockerfile`: ele congelava a
URL da API na imagem em build time — e é por não repetir esse erro que o
repasse vive em `proxy.ts`, não em `rewrites`.

## Consequências

**A API não precisa ganhar CORS** e o cookie **continua `SameSite=Lax`**. A
decisão de segurança documentada no ADR 0001 e no docstring de `_setar_cookie`
fica intacta — este ADR contorna o problema em vez de afrouxar a proteção.

**A URL da API deixa de ser configuração de build e vira configuração de
servidor.** A mesma imagem Docker serve para qualquer ambiente; muda-se
`API_URL`, não a imagem. Some também a classe de incidente em que uma imagem
promovida carrega, congelada no bundle, a URL do ambiente onde foi construída.

**O Next passa a ser dependência de runtime no caminho de toda chamada.** O
front deixa de ser um monte de arquivos estáticos: se o servidor do Next cai,
não é só a renderização que para — as chamadas à API param junto, porque
passam por ele. Ganha-se, em troca, um ponto único onde caberá o que um BFF
faria (cabeçalhos, timeout, observabilidade).

**O `proxy.ts` roda antes do filesystem.** A doc local do Next
(`node_modules/next/dist/docs/01-app/03-api-reference/03-file-conventions/proxy.md`)
ordena a resolução com o Proxy **antes** de `beforeFiles`, do filesystem e dos
rewrites de configuração. Ao contrário do que aconteceria com `rewrites`, uma
Route Handler futura em `apps/web/app/api/algo` **não** sequestraria o caminho:
o proxy o alcança primeiro. Para abrir exceção a um caminho será preciso
tratá-la explicitamente no `proxy.ts` ou no seu `matcher` — o que é a forma
desejável, porque fica visível no código em vez de depender de precedência
implícita.

**Um salto de rede a mais por requisição.** Irrelevante perto do custo do
sistema, que é a extração por Vision (segundos por página).

## Alternativas consideradas

**CORS na API + `SameSite=None; Secure`.** É o caminho direto: liberar a
origem do front e afrouxar o cookie para viajar cross-site. Descartada porque
reverteria uma decisão de segurança deliberada e documentada — o `Lax` está lá
como defesa de CSRF, e trocá-lo por `None` obrigaria a introduzir proteção
CSRF explícita para recuperar o que já se tinha de graça. Além disso,
`SameSite=None` exige `Secure`, e `Secure` exige HTTPS: o desenvolvimento
local em `http://localhost` pararia de funcionar, ou dependeria de certificado
de desenvolvimento. Custo alto para resolver algo que o proxy resolve com uma
entrada de configuração.

**Route Handlers do Next como BFF completo.** Escrever, à mão, um handler no
Next para cada endpoint da API. É a issue #29 ressuscitada — que já foi fechada
como `not planned` quando o ADR 0001 levou a autenticação para a API. Seriam
cerca de 30 handlers para manter em sincronia com o backend, cada um uma
oportunidade de divergir do contrato, sem ganho nenhum enquanto o que se
precisa é apenas repassar a requisição. Continua sendo a evolução natural
**se** e quando houver lógica de verdade para colocar entre o navegador e a
API: o `proxy.ts` não fecha essa porta — basta o `matcher` deixar de casar o
caminho que passar a ter handler próprio, ou o próprio proxy devolver
`NextResponse.next()` para ele. A diferença é que a exceção fica escrita, e não
depende de ordem de resolução implícita.
