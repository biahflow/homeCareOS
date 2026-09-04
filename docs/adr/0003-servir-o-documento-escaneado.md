# ADR 0003 — Servir o documento escaneado pela API

- **Status:** aceito
- **Data:** 2026-09-04
- **Issue:** #51

## Contexto

O produto é conferência de evolução de prontuário: alguém compara o que a
extração leu com o que está no papel. Até aqui, **a interface não tinha como
mostrar o papel**.

`documentos.arquivo_url` guarda a **chave** do objeto no storage
(`documentos/{uuid}/{sha256}.png`, montada por `storage.build_key`), não uma
URL — o nome mente, e o contrato tipado do front já registrava a armadilha em
`packages/contracts/src/tipos.ts`. Nenhum endpoint servia o arquivo, então a
conferente via apenas os campos extraídos, inclusive quando a confiança de um
campo vinha baixa, que é exatamente a hora em que ela precisaria olhar o
original.

Havia dois caminhos: devolver uma **URL assinada** do storage para o navegador
buscar o objeto direto, ou **a API transmitir os bytes**.

## Decisão

**A API serve o arquivo, em streaming**, por
`GET /api/documentos/{documento_id}/arquivo`.

- `StreamingResponse` sobre um iterador de blocos de 64 KiB
  (`storage.CHUNK_SIZE`): o pico de memória por download é constante, e não
  proporcional ao tamanho da evolução escaneada vezes o número de pessoas
  conferindo ao mesmo tempo;
- `Content-Type` deduzido da extensão da chave — a extensão é escolhida na
  gravação a partir do content type real da página, e é a única informação que
  sobrevive ao armazenamento (o `LocalDocumentStorage` não guarda content type,
  e o banco não tem coluna para ele). Extensão desconhecida vira
  `application/octet-stream`, nunca um tipo adivinhado;
- `Content-Disposition: inline`, com nome legível montado do documento
  (`evolucao-2026-08-pagina-3.png`) e filtrado para `[A-Za-z0-9._-]` — a
  conferência é olhar, não baixar, e nome de arquivo que entra em header
  precisa ser inerte;
- **404 nos dois casos**: documento inexistente e documento cujo arquivo não
  está no storage. Um arquivo que sumiu do bucket não é defeito da aplicação
  (500), é o objeto que não está lá; `StorageError` de infraestrutura continua
  virando 503 no handler global;
- a autorização é a que o router já aplica (`exigir_papel(*todos_os_papeis)`
  no `include_router` de `main.py`): ler documento é dos três papéis, e servir
  o arquivo é leitura de documento;
- **nada é logado**: a chave identifica o objeto de prontuário no bucket, e o
  conteúdo é o prontuário.

O `DocumentStorage` Protocol ganhou a leitura, implementada nos dois backends
(`S3DocumentStorage` e `LocalDocumentStorage`). O contrato do `get` tem uma
exigência que não é detalhe de implementação: **a procura acontece na chamada,
não na primeira iteração**. Um `get` que fosse ele próprio um gerador só
descobriria a chave ausente dentro do corpo de uma resposta com status já
enviado — e "o arquivo não está no storage" não teria como virar 404. É a mesma
classe de armadilha que `reports.router._stream_csv` documenta do outro lado,
com a sessão do banco já fechada quando o corpo começa a sair; por isso o
handler lê chave, content type e nome para variáveis locais **antes** de montar
a resposta, e o iterador entregue ao `StreamingResponse` não toca no ORM.

## Consequências

**A API entra no caminho dos bytes.** Cada documento aberto ocupa um worker
enquanto transmite, e a banda do prontuário passa a ser banda da API. É o custo
aceito: a alternativa exigiria infraestrutura que não existe (ver abaixo). Não
há CDN nem cache nesta entrega — `ETag`/`Cache-Control` e range requests são
trabalho futuro, com a sua própria issue.

**O arquivo fica atrás da mesma autorização do resto.** Revogar uma sessão
(`usuarios.ativo = false`, ADR 0001) corta o acesso ao documento na requisição
seguinte. Uma URL assinada continuaria válida até expirar, fora de qualquer
sessão — e isto é prontuário clínico.

**O storage ganhou uma capacidade que outras trilhas herdam.** `get` está no
Protocol, não no endpoint: quem precisar reprocessar uma extração a partir da
página já gravada não precisa de outro caminho de leitura.

**`arquivo_url` continua com o nome errado, e de propósito.** Renomear um campo
de resposta é quebra de contrato, e o contrato tipado (`packages/contracts`) e
a tela que o consome (`apps/web`) estão fora do escopo desta entrega. Um rename
só na API deixaria o TypeScript declarando um campo que a API parou de mandar —
quebra silenciosa, que aparece como `undefined` na tela e não no build. O campo
ganhou descrição explícita no OpenAPI ("chave do objeto, não uma URL"), e o
endpoint novo remove o motivo de alguém tentar usá-lo como endereço. O rename
cabe numa mudança que altere API, contrato e front juntos.

## Alternativas consideradas

**URL assinada do storage (presigned URL), com o navegador buscando o objeto
direto.** É a solução de manual, e não funciona aqui — por duas razões
verificadas no código e na configuração, não supostas:

1. **O presigned do S3/MinIO aponta para a rede interna do Compose.**
   `S3DocumentStorage.presigned_url` assina com um cliente boto3 construído
   sobre `settings.s3_endpoint_url`, e no Compose o serviço `api` recebe
   `S3_ENDPOINT_URL: http://minio:9000` (docker-compose.yml, linhas 56 e 109).
   `minio:9000` é nome de serviço da rede interna: o navegador não o resolve. E
   não adianta trocar o host pela porta publicada no hospedeiro (9002): o
   `host` entra na assinatura SigV4, e reescrevê-lo invalida a URL. Serviria só
   com um endpoint público de MinIO, que não existe neste projeto.
2. **O presigned do storage local devolve `file://`.**
   `LocalDocumentStorage.presigned_url` devolve `f"file://{caminho}"` — um
   caminho no disco do processo da API, inútil para o navegador. É o backend
   que roda em desenvolvimento sem credencial, e ele quebraria de um jeito
   diferente do de produção, que é a pior forma de quebrar.

Some-se a isso o que já está em "Consequências": a URL assinada tira o arquivo
de trás da sessão pelo tempo da expiração. As três razões apontam para o mesmo
lado.

**Servir o arquivo pelo proxy do Next (`apps/web/proxy.ts`) direto do MinIO.**
Trocaria o problema de lugar sem resolvê-lo: o servidor do Next alcança a rede
interna, mas passaria a precisar de credencial de S3 e a decidir sozinho quem
pode ver qual documento — autorização de prontuário duplicada em duas
linguagens, com o front virando o segundo dono da regra. O ADR 0002 fez o
caminho oposto de propósito: o Next repassa, a API decide.

**Embutir o arquivo em base64 no JSON de `GET /api/documentos/{id}`.**
Dispensaria endpoint novo e quebraria tudo o que o streaming protege: carrega o
documento inteiro na memória, engorda 33%, impede transmissão incremental e
mistura no mesmo payload o dado de conferência (que a tela lista) com o binário
(que ela só busca quando alguém quer ver).
