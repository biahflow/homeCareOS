import { ApiError } from "./erros";
import type {
  AtualizarPendenciaParams,
  BaselineOut,
  BaselineUpsert,
  DocumentoDetalhe,
  DocumentoListItem,
  EsqueciSenhaParams,
  EuResposta,
  FiltrosConferencia,
  LinhaConferencia,
  ListarDocumentosParams,
  ListarPendenciasParams,
  LoginParams,
  LoginResposta,
  MetricasParams,
  MetricasResponse,
  MfaCodigosRecuperacaoOut,
  MfaConfirmarParams,
  MfaDesativarParams,
  MfaIniciarOut,
  MfaReemitirCodigosParams,
  MfaVerificarParams,
  Operadora,
  PendenciaItem,
  RedefinirSenhaParams,
  RelatorioConferenciaParams,
  RespostaPaginada,
  ResumoPendencias,
  RevalidacaoResponse,
  UploadParams,
  UploadResponse,
  UsuarioOut,
} from "./tipos";

/**
 * Mensagem de erro pronta para exibição, tirada do envelope padrão da API.
 *
 * Todo erro da API sai como `{"error": {"tipo", "mensagem", "detalhes"}}` — a
 * convenção está em `apps/api/src/homecareos/api/responses.py` e é aplicada por
 * exception handler global, então vale para 401, 422, 429 e 500 igualmente.
 *
 * A mensagem sai como a API a escreveu, sem enriquecer: o 401 do login é o
 * mesmo para e-mail inexistente e senha errada **de propósito**, e deduzir qual
 * dos dois foi entregaria a quem sonda a lista de quem trabalha na operação.
 */
function extrairMensagem(corpo: unknown, status: number): string {
  if (typeof corpo === "object" && corpo !== null && "error" in corpo) {
    const erro = (corpo as { error?: unknown }).error;
    if (
      typeof erro === "object" &&
      erro !== null &&
      "mensagem" in erro &&
      typeof (erro as { mensagem?: unknown }).mensagem === "string"
    ) {
      return (erro as { mensagem: string }).mensagem;
    }
  }
  return `Falha na requisição (HTTP ${status}).`;
}

export interface OpcoesRequisicao {
  /**
   * Header `Cookie` a repassar quando a chamada parte do **servidor** (Server
   * Component do Next), onde não existe cookie jar do navegador.
   *
   * No navegador este campo não é usado: `Cookie` é um header proibido para o
   * `fetch`, e quem o envia é o próprio navegador por causa de
   * `credentials: "include"` abaixo.
   */
  cookie?: string;
}

/**
 * Faz a requisição e devolve a `Response`, ou lança {@link ApiError}.
 *
 * Um lugar só para três garantias que não podem depender de quem escreve a
 * próxima função: `credentials: "include"`, erro de rede virando `ApiError` com
 * `status: 0`, e resposta de erro virando `ApiError` com a mensagem da API.
 */
async function requisitar(
  baseUrl: string,
  caminho: string,
  init: RequestInit,
  opcoes?: OpcoesRequisicao,
): Promise<Response> {
  const headers = new Headers(init.headers);
  if (opcoes?.cookie) {
    headers.set("Cookie", opcoes.cookie);
  }

  let response: Response;
  try {
    response = await fetch(`${baseUrl}${caminho}`, {
      ...init,
      headers,
      // NÃO REMOVER. Sem `credentials: "include"` o `fetch` não envia nem grava
      // o cookie `httpOnly` da sessão: o login responde 200, o navegador
      // descarta o `Set-Cookie`, e a aplicação passa a parecer autenticada sem
      // ter autenticado ninguém. É a linha que parece supérflua porque as
      // chamadas são same-origin (o proxy do ADR 0002) — e o default
      // `same-origin` de fato mandaria o cookie hoje. Ela está explícita para
      // que a garantia não dependa de um default que a plataforma pode mudar,
      // nem de o front continuar chamando pela mesma origem.
      credentials: "include",
    });
  } catch (cause) {
    throw new ApiError({
      status: 0,
      message: "Não foi possível conectar à API. Verifique se o serviço está no ar.",
      detail: cause,
    });
  }

  if (!response.ok) {
    let detail: unknown;
    try {
      detail = await response.json();
    } catch {
      detail = await response.text().catch(() => undefined);
    }
    throw new ApiError({
      status: response.status,
      message: extrairMensagem(detail, response.status),
      detail,
    });
  }

  return response;
}

/**
 * Envia um documento para a API como `multipart/form-data`, identificando a
 * tentativa pelo header `Idempotency-Key` para que reenvios do mesmo arquivo
 * não dupliquem documentos.
 *
 * Nunca lança string solta: erros de rede ou HTTP viram {@link ApiError}.
 */
export async function uploadDocumento(
  baseUrl: string,
  params: UploadParams,
): Promise<UploadResponse> {
  const formData = new FormData();
  formData.append("arquivo", params.arquivo);
  formData.append("competencia", params.competencia);

  // Sem `Content-Type` explícito: quem o define — com o `boundary` do
  // multipart — é o próprio `fetch`, a partir do `FormData`.
  const response = await requisitar(baseUrl, "/api/documentos", {
    method: "POST",
    headers: { "Idempotency-Key": params.idempotencyKey },
    body: formData,
  });

  return (await response.json()) as UploadResponse;
}

/**
 * `POST /api/auth/login`. Sucesso devolve o usuário **ou**
 * `{mfa_pendente: true}` — use `ehUsuario` para distinguir, e não confie na
 * ausência de erro para desenhar a área logada.
 *
 * 401 (credencial inválida, com a mesma mensagem para qualquer motivo) e 429
 * (bloqueio por tentativas) chegam como {@link ApiError} com o `status`.
 */
export async function login(baseUrl: string, params: LoginParams): Promise<LoginResposta> {
  const response = await requisitar(baseUrl, "/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  return (await response.json()) as LoginResposta;
}

/**
 * `POST /api/auth/mfa/verificar`: completa o login apresentando o segundo
 * fator, lido da sessão **pendente** que o cookie carrega.
 *
 * 401 significa código errado **ou** sessão pendente que não existe mais
 * (expirada, revogada, substituída por outro login). A API não distingue os
 * dois de propósito, e o cliente também não tem como — quem trata decide pelo
 * contexto da tela.
 */
export async function verificarMfa(
  baseUrl: string,
  params: MfaVerificarParams,
): Promise<UsuarioOut> {
  const response = await requisitar(baseUrl, "/api/auth/mfa/verificar", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  return (await response.json()) as UsuarioOut;
}

/**
 * `POST /api/auth/mfa/iniciar`: gera o segredo TOTP e **não ativa nada**.
 *
 * **Chamar de novo SUBSTITUI o segredo anterior** (`auth/router.py:iniciar_mfa`
 * — "chamar de novo o **substitui**"). Isto não é detalhe de implementação, é o
 * que decide onde esta função pode ser chamada: se ela rodar no carregamento da
 * tela, cada recarga invalida o QR code que a pessoa acabou de escanear, e ela
 * descobre isso como um 422 em {@link confirmarMfa} sem nenhuma pista da causa.
 * **Só por ação explícita de quem está cadastrando**, uma vez — nunca em
 * `useEffect` de montagem, nunca em render de Server Component (que, além
 * disso, colocaria o segredo no payload RSC e no cache de rotas do cliente).
 *
 * 409 significa que o segundo fator **já está ativado** nesta conta: a API se
 * recusa a trocar o segredo de quem já o usa, porque uma sessão sequestrada
 * faria essa troca sem provar nada. Como `GET /api/auth/eu` não expõe
 * `mfa_ativado`, este 409 é a única forma de a interface descobrir o estado — é
 * resposta esperada, e não falha.
 */
export async function iniciarMfa(baseUrl: string): Promise<MfaIniciarOut> {
  const response = await requisitar(baseUrl, "/api/auth/mfa/iniciar", { method: "POST" });
  return (await response.json()) as MfaIniciarOut;
}

/**
 * `POST /api/auth/mfa/confirmar`: prova que o app guardou o segredo, ativa o
 * segundo fator e devolve os códigos de recuperação — **a única vez em que eles
 * existem em claro**. Ver {@link MfaCodigosRecuperacaoOut}.
 *
 * 422 tem **duas** causas que o chamador não consegue distinguir, e a segunda é
 * a que não ocorre a ninguém: o código pode estar errado, ou o segredo gravado
 * pode ter sido substituído por uma chamada posterior a {@link iniciarMfa} —
 * outra aba, outro dispositivo, a mesma pessoa recomeçando o cadastro. Quem
 * trata precisa nomear as duas e oferecer o caminho de gerar um QR code novo;
 * apresentar só "código incorreto" manda a pessoa digitar de novo, para sempre,
 * um código que nunca vai valer.
 */
export async function confirmarMfa(
  baseUrl: string,
  params: MfaConfirmarParams,
): Promise<MfaCodigosRecuperacaoOut> {
  const response = await requisitar(baseUrl, "/api/auth/mfa/confirmar", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  return (await response.json()) as MfaCodigosRecuperacaoOut;
}

/**
 * `POST /api/auth/mfa/desativar`: desliga o segundo fator apresentando senha
 * **e** código. Responde 204, sem corpo.
 *
 * 422 é o **mesmo** para senha errada e para código errado, de propósito
 * (`MfaDesativarRequest`: "duas mensagens diriam qual dos dois o atacante já
 * tem"). Quem trata não deve tentar deduzir qual dos dois falhou nem separar as
 * mensagens — não há informação aqui para isso, e inventá-la desfaria a
 * proteção.
 *
 * A operação é destrutiva e silenciosa: apaga o segredo, a flag e **os códigos
 * de recuperação**. Não há como voltar atrás; religar é {@link iniciarMfa} de
 * novo, com segredo e códigos novos — e o aplicativo autenticador precisa ser
 * cadastrado de novo, porque o segredo é outro.
 *
 * Quem só quer uma lista nova de códigos de recuperação **não precisa passar
 * por aqui**: {@link reemitirCodigosMfa} troca a lista sem tocar no segredo.
 */
export async function desativarMfa(baseUrl: string, params: MfaDesativarParams): Promise<void> {
  await requisitar(baseUrl, "/api/auth/mfa/desativar", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
}

/**
 * `POST /api/auth/mfa/reemitir-codigos`: troca **toda** a lista de códigos de
 * recuperação por uma nova, sem desativar o segundo fator (issue #39).
 *
 * O segredo TOTP não muda: o aplicativo autenticador cadastrado continua
 * valendo, e não há QR code para escanear de novo.
 *
 * **Os códigos anteriores morrem, usados e não usados.** Quem chama precisa
 * dizer isso a quem confirma **antes** da confirmação, e não depois: alguém que
 * guardou a lista velha num cofre precisa saber que ela virou papel sem valor.
 *
 * A resposta é {@link MfaCodigosRecuperacaoOut} e vale para ela tudo o que vale
 * para a da ativação: é a única vez que estes códigos existem em claro, e não
 * se persiste nada disso no cliente.
 *
 * 422 é o **mesmo** para senha errada e para código errado, de propósito — não
 * tente deduzir qual dos dois falhou nem separar as mensagens. 409 significa
 * que o segundo fator não está ativado nesta conta (não há lista a reemitir).
 *
 * 429 merece atenção de quem desenha a tela: as falhas aqui contam em
 * `tentativas_login` com o e-mail da pessoa, como as de `/mfa/verificar`.
 * Errar senha ou código muitas vezes **tranca o login dela** pela janela
 * configurada — é o preço de não deixar esta rota ser sondada de graça.
 */
export async function reemitirCodigosMfa(
  baseUrl: string,
  params: MfaReemitirCodigosParams,
): Promise<MfaCodigosRecuperacaoOut> {
  const response = await requisitar(baseUrl, "/api/auth/mfa/reemitir-codigos", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  return (await response.json()) as MfaCodigosRecuperacaoOut;
}

/**
 * `POST /api/auth/logout`: revoga a sessão **no servidor** e apaga o cookie.
 *
 * Limpar só o estado do cliente não é logout: a sessão continuaria válida por
 * até 12h para quem tivesse o cookie — e estação compartilhada é o caso real
 * desta operação. É idempotente: sem cookie, ou com cookie já revogado,
 * responde 204 do mesmo jeito.
 */
export async function logout(baseUrl: string): Promise<void> {
  await requisitar(baseUrl, "/api/auth/logout", { method: "POST" });
}

/**
 * `GET /api/auth/eu`: quem está autenticado nesta requisição.
 *
 * **401 não distingue "sem sessão" de "sessão pendente de MFA"** — as duas
 * chegam aqui como {@link ApiError} com `status: 401`, porque
 * `sessoes.resolver_sessao` recusa a sessão pendente exatamente como recusa a
 * inexistente. Não há como perguntar à API se falta o segundo fator, e não
 * existe estado no cliente que possa responder isso sem divergir dela.
 */
export async function obterUsuarioAtual(
  baseUrl: string,
  opcoes?: OpcoesRequisicao,
): Promise<EuResposta> {
  const response = await requisitar(
    baseUrl,
    "/api/auth/eu",
    {
      method: "GET",
      // Identidade não se cacheia. O Next já trata como dinâmica a rota que lê
      // `cookies()`, mas a garantia não pode depender de o chamador ter lido o
      // cookie antes desta chamada.
      cache: "no-store",
    },
    opcoes,
  );
  return (await response.json()) as EuResposta;
}

/**
 * `POST /api/auth/senha/esqueci`: pede o link de redefinição por e-mail.
 *
 * **Responde 204 sempre**, e é o contrato — não uma simplificação. E-mail
 * cadastrado, inexistente, de conta desativada, com teto de envios já
 * atingido na hora, ou com o SMTP desligado neste ambiente: todos voltam com
 * o mesmo 204 vazio (`auth/router.py:esqueci_senha`). Não existe leitura da
 * resposta que diga qual foi o caso — quem chama não deve tentar, e a tela
 * que consome isto precisa mostrar a mesma mensagem para qualquer e-mail
 * digitado, existente ou não.
 */
export async function esqueciSenha(baseUrl: string, params: EsqueciSenhaParams): Promise<void> {
  await requisitar(baseUrl, "/api/auth/senha/esqueci", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
}

/**
 * `POST /api/auth/senha/redefinir`: troca a senha usando o token de uso único
 * recebido por e-mail.
 *
 * Sucesso (204) revoga **todas** as sessões do usuário — inclusive a que fez
 * esta chamada. Não há sessão para manter depois disto; quem trata o sucesso
 * deve mandar a pessoa para `/login`, não tentar preservar estado de sessão.
 *
 * **Os dois 422 são indistinguíveis para quem chama.** O `tipo` do envelope
 * de erro vem do status HTTP (`api/errors.py:_tipo_do_status`), não da causa:
 * token inexistente/expirado/já usado e senha fraca chegam aqui como o mesmo
 * {@link ApiError} com `status: 422` — só a `message` (texto que a própria
 * API escreveu) diz qual foi. A diferença importa: com senha fraca o token
 * **continua válido**, porque a validação de força roda antes de marcar o
 * token como usado (`auth/recuperacao.py`, docstring do módulo); com token
 * inválido, uma nova tentativa não adianta. Quem trata não deve classificar a
 * causa pelo texto — só exibir `erro.message` e manter o formulário
 * utilizável para uma segunda tentativa.
 */
export async function redefinirSenha(
  baseUrl: string,
  params: RedefinirSenhaParams,
): Promise<void> {
  await requisitar(baseUrl, "/api/auth/senha/redefinir", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
}

/* Pendências e operadoras — a fila de conferência. */

/**
 * Monta a query string ignorando o que não foi informado.
 *
 * `undefined` vira **ausência** do parâmetro, e não a string `"undefined"`: a
 * API valida cada filtro pelo tipo, então `status=undefined` seria 422 em vez
 * de "sem filtro" — um erro que aparece só quando alguém navega sem filtrar.
 */
function queryString(params: Record<string, string | number | boolean | undefined>): string {
  const busca = new URLSearchParams();
  for (const [chave, valor] of Object.entries(params)) {
    if (valor !== undefined) {
      busca.set(chave, String(valor));
    }
  }
  const texto = busca.toString();
  return texto === "" ? "" : `?${texto}`;
}

/**
 * `GET /api/pendencias`: a página atual da fila de conferência.
 *
 * Legível pelos três papéis — quem transiciona é que é restrito, ver
 * {@link atualizarPendencia}.
 *
 * Dois detalhes do contrato que mudam o que a interface deve mostrar:
 *
 * - `deadline` filtra por "até esta data **inclusive**": a API converte a data
 *   para o fim do dia (`time.max`), então `deadline=2026-09-04` inclui o que
 *   vence às 23h de 4 de setembro.
 * - `paginacao.total` é o total **filtrado**. Ele não muda entre páginas do
 *   mesmo filtro, e é ele — não `data.length` — que diz se há próxima página.
 *
 * `cache: "no-store"` não é otimização às avessas: a resposta carrega descrição
 * de problema de prontuário, e uma fila de trabalho compartilhada que volta do
 * cache mostra a alguém uma pendência que outra pessoa já pegou.
 */
export async function listarPendencias(
  baseUrl: string,
  params: ListarPendenciasParams = {},
  opcoes?: OpcoesRequisicao,
): Promise<RespostaPaginada<PendenciaItem>> {
  const response = await requisitar(
    baseUrl,
    `/api/pendencias${queryString({
      status: params.status,
      deadline: params.deadline,
      operadora_id: params.operadora_id,
      limite: params.limite,
      offset: params.offset,
    })}`,
    { method: "GET", cache: "no-store" },
    opcoes,
  );
  return (await response.json()) as RespostaPaginada<PendenciaItem>;
}

/**
 * `GET /api/pendencias/resumo`: as contagens do topo da tela.
 *
 * **Não aceita filtro**: o resumo é sempre da operação inteira, e não do que a
 * listagem está mostrando. Apresentá-lo colado a uma lista filtrada, sem dizer
 * isso, faz alguém ler os dois números como se fossem do mesmo conjunto.
 *
 * Ver {@link ResumoPendencias} para a segunda armadilha: `por_status` conta
 * tudo, `por_faixa_deadline` só o que está em aberto.
 */
export async function resumoPendencias(
  baseUrl: string,
  opcoes?: OpcoesRequisicao,
): Promise<ResumoPendencias> {
  const response = await requisitar(
    baseUrl,
    "/api/pendencias/resumo",
    { method: "GET", cache: "no-store" },
    opcoes,
  );
  return (await response.json()) as ResumoPendencias;
}

/**
 * `PATCH /api/pendencias/{id}`: avança a pendência uma etapa do ciclo.
 *
 * Três respostas de erro que quem trata **precisa** distinguir, porque exigem
 * reações diferentes:
 *
 * - **403** — o papel `gestor` lê a operação inteira mas não faz conferência
 *   (ADR 0001), e o endpoint exige conferente ou coordenador. Esconder o botão
 *   para o gestor é ergonomia; a autoridade é esta resposta, e a interface tem
 *   que continuar de pé quando ela chega (papel alterado no servidor no meio do
 *   turno, sessão de outra pessoa, aba antiga).
 * - **422** — a transição pedida não é válida a partir do status **atual no
 *   banco**. Na fila de conferência esse é o caso comum, não a exceção: várias
 *   conferentes trabalham a mesma lista, e a segunda a clicar encontra a
 *   pendência já movida. Não é erro de quem clicou, e apresentá-lo como falha
 *   genérica faz a pessoa tentar de novo contra um estado que não existe mais —
 *   o que cabe é dizer que mudou e recarregar a lista.
 * - **404** — a pendência não existe (id de uma lista velha).
 *
 * O sucesso devolve a pendência **já transicionada**, mas ela não é o retrato
 * completo da tela: o mesmo PATCH pode mover o documento e disparar a
 * revalidação, e nada disso aparece aqui. Depois de um sucesso, releia a
 * listagem em vez de remendar o item na mão.
 */
export async function atualizarPendencia(
  baseUrl: string,
  pendenciaId: string,
  params: AtualizarPendenciaParams,
  opcoes?: OpcoesRequisicao,
): Promise<PendenciaItem> {
  const response = await requisitar(
    baseUrl,
    `/api/pendencias/${encodeURIComponent(pendenciaId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    },
    opcoes,
  );
  return (await response.json()) as PendenciaItem;
}

/**
 * `GET /api/operadoras`: as operadoras cadastradas, para o filtro da fila.
 *
 * Lista simples, **sem paginação e sem envelope** — devolve o array direto. Não
 * é esquecimento da API: são os convênios atendidos pela empresa, um cadastro
 * pequeno.
 */
export async function listarOperadoras(
  baseUrl: string,
  opcoes?: OpcoesRequisicao,
): Promise<Operadora[]> {
  const response = await requisitar(
    baseUrl,
    "/api/operadoras",
    { method: "GET", cache: "no-store" },
    opcoes,
  );
  return (await response.json()) as Operadora[];
}

/* Documentos em conferência — `/api/documentos` (issue #6). */

const CAMINHO_DOCUMENTOS = "/api/documentos";

/**
 * `GET /api/documentos`: a página atual dos documentos em conferência.
 *
 * Legível pelos **três** papéis; quem revalida é que é restrito, ver
 * {@link revalidarDocumento}.
 *
 * A API ordena por `created_at` **decrescente** (o mais novo primeiro) e o
 * cliente não reordena: um documento recém-enviado tem que aparecer no topo da
 * primeira página, que é onde quem acabou de enviar vai olhar.
 *
 * `paginacao.total` é o total **filtrado**. Ele não muda entre páginas do mesmo
 * filtro, e é ele — não `data.length` — que diz se há próxima página.
 *
 * `cache: "no-store"` não é otimização às avessas: a lista é de uma fila de
 * trabalho compartilhada, e uma resposta que volta do cache mostra a alguém um
 * documento que outra pessoa já moveu.
 */
export async function listarDocumentos(
  baseUrl: string,
  params: ListarDocumentosParams = {},
  opcoes?: OpcoesRequisicao,
): Promise<RespostaPaginada<DocumentoListItem>> {
  const response = await requisitar(
    baseUrl,
    `${CAMINHO_DOCUMENTOS}${queryString({
      competencia: params.competencia,
      status: params.status,
      operadora_id: params.operadora_id,
      paciente_id: params.paciente_id,
      limite: params.limite,
      offset: params.offset,
    })}`,
    { method: "GET", cache: "no-store" },
    opcoes,
  );
  return (await response.json()) as RespostaPaginada<DocumentoListItem>;
}

/**
 * `GET /api/documentos/{id}`: o documento com a extração e as validações.
 *
 * **404 é resposta esperada**, não falha: o id vem da URL, e qualquer endereço
 * colado pode apontar para um documento que não existe. Quem chama distingue
 * pelo `status` do {@link ApiError} e mostra a mensagem própria — um erro
 * genérico faria a pessoa procurar defeito no sistema em vez de no link.
 *
 * A resposta carrega prontuário inteiro (`campos_extraidos`): daí o
 * `cache: "no-store"`, e daí `campos_extraidos` nunca ir para `console.log`
 * nem para query string.
 */
export async function obterDocumento(
  baseUrl: string,
  documentoId: string,
  opcoes?: OpcoesRequisicao,
): Promise<DocumentoDetalhe> {
  const response = await requisitar(
    baseUrl,
    `${CAMINHO_DOCUMENTOS}/${encodeURIComponent(documentoId)}`,
    { method: "GET", cache: "no-store" },
    opcoes,
  );
  return (await response.json()) as DocumentoDetalhe;
}

/**
 * Caminho de `GET /api/documentos/{id}/arquivo`: a página escaneada, servida
 * em streaming pela própria API (issue #51, PR #54) — não mais uma URL
 * assinada do storage, e não mais um endpoint inexistente.
 *
 * **Isto monta um caminho, não faz a chamada.** Quem busca o arquivo é o
 * navegador — via `<img src>` ou `<a href target="_blank">` — não este
 * cliente: é o navegador que precisa mandar o cookie de sessão, e um `fetch`
 * daqui não colocaria os bytes em lugar nenhum útil. Por isso a assinatura
 * segue {@link urlRelatorioConferenciaCsv}, não {@link obterDocumento}.
 *
 * `baseUrl` existe pela mesma razão de sempre (ADR 0002): quem chama do
 * navegador passa a string vazia de `lib/env.ts:API_BASE_URL`, para o
 * resultado ser um caminho relativo que `apps/web/proxy.ts` repassa. Passar a
 * URL direta da API aqui vazaria endereço de servidor para o navegador e
 * pularia o proxy que carrega o cookie.
 */
export function caminhoArquivoDocumento(baseUrl: string, documentoId: string): string {
  return `${baseUrl}${CAMINHO_DOCUMENTOS}/${encodeURIComponent(documentoId)}/arquivo`;
}

/**
 * `POST /api/documentos/{id}/revalidar`: reaplica as regras ativas sobre a
 * extração **já existente** e reclassifica o documento.
 *
 * Não chama o provider de Vision de novo — a extração custa dinheiro e o
 * documento não mudou; o que mudou foram as regras.
 *
 * Três erros que quem trata **precisa** distinguir, porque exigem reações
 * diferentes:
 *
 * - **403** — exige conferente ou coordenador. **A autorização aqui é a da fila
 *   de pendências, não a dos relatórios**: revalidar é ação de conferência, e o
 *   gestor lê a operação sem fazê-la (ADR 0001). Esconder o botão para ele é
 *   ergonomia; a autoridade é esta resposta, e a tela tem que continuar de pé
 *   quando ela chega (papel alterado no servidor, aba antiga).
 * - **409** — e não 422: o corpo está correto, é o **estado do documento** que
 *   impede revalidar agora. São quatro causas, e a API as escreve em `message`:
 *   documento sem operadora, sem extração, extração ilegível, operadora sem
 *   regras ativas, ou documento em status terminal. Nenhuma delas se resolve
 *   tentando de novo, e todas se resolvem em outro lugar do sistema — exibir a
 *   frase da API é o que diz onde.
 * - **404** — o documento não existe (id de uma lista velha, endereço colado).
 *
 * O sucesso devolve o status **depois** da reclassificação, que pode ser pior
 * que o anterior. Ele também não é o retrato completo: a revalidação abre e
 * fecha pendências, e nada disso está aqui — releia o documento em vez de
 * remendar a tela na mão.
 */
export async function revalidarDocumento(
  baseUrl: string,
  documentoId: string,
  opcoes?: OpcoesRequisicao,
): Promise<RevalidacaoResponse> {
  const response = await requisitar(
    baseUrl,
    `${CAMINHO_DOCUMENTOS}/${encodeURIComponent(documentoId)}/revalidar`,
    { method: "POST" },
    opcoes,
  );
  return (await response.json()) as RevalidacaoResponse;
}

/* Relatórios e métricas — `/api/relatorios` (issue #8). */

const CAMINHO_CONFERENCIA = "/api/relatorios/conferencia";

/**
 * Os filtros do relatório na forma de query params — **um lugar só**, usado
 * pela listagem JSON e pela URL do CSV.
 *
 * É o que garante que o arquivo baixado contenha exatamente as linhas que a
 * tela está mostrando: dois montadores de query divergiriam no primeiro filtro
 * novo, e o CSV passaria a mentir sobre o que exportou sem nenhum erro visível.
 *
 * `apenas_pendentes` só entra quando **ligado**: `false` é o default da API, e
 * omiti-lo mantém a barra de endereços legível.
 */
function parametrosDaConferencia(
  filtros: FiltrosConferencia,
): Record<string, string | number | boolean | undefined> {
  return {
    competencia: filtros.competencia,
    status: filtros.status,
    operadora_id: filtros.operadora_id,
    paciente_id: filtros.paciente_id,
    data_inicio: filtros.data_inicio,
    data_fim: filtros.data_fim,
    apenas_pendentes: filtros.apenas_pendentes === true ? true : undefined,
  };
}

/**
 * `GET /api/relatorios/conferencia`: a página atual do relatório operacional.
 *
 * Legível pelos **três** papéis — é a lista que a conferente usa todo dia. As
 * métricas agregadas ({@link metricas}) é que são leitura de gestão.
 *
 * Cada linha traz `severidade` decidida pela API. Ver {@link LinhaConferencia}:
 * quem consome traduz severidade em estilo e **não** deriva cor do `status`.
 *
 * `cache: "no-store"` não é otimização às avessas: a resposta carrega nome de
 * paciente e descrição de pendência, e uma tela compartilhada que volta do cache
 * mostra a alguém o retrato de outra pessoa.
 */
export async function relatorioConferencia(
  baseUrl: string,
  params: RelatorioConferenciaParams = {},
  opcoes?: OpcoesRequisicao,
): Promise<RespostaPaginada<LinhaConferencia>> {
  const response = await requisitar(
    baseUrl,
    `${CAMINHO_CONFERENCIA}${queryString({
      ...parametrosDaConferencia(params),
      limite: params.limite,
      offset: params.offset,
    })}`,
    { method: "GET", cache: "no-store" },
    opcoes,
  );
  return (await response.json()) as RespostaPaginada<LinhaConferencia>;
}

/**
 * A URL de `GET /api/relatorios/conferencia.csv` com estes filtros — **só a
 * string**, sem requisição nenhuma.
 *
 * O download é um link comum (`<a href>`) apontando para cá, e isso é decisão,
 * não preguiça:
 *
 * - a URL é same-origin (o proxy do ADR 0002), então o cookie de sessão viaja
 *   sozinho e não há credencial para montar aqui;
 * - o navegador cuida do download, do nome do arquivo (`Content-Disposition`) e
 *   de escrever direto em disco;
 * - a API transmite o CSV em blocos, com uma sessão de banco própria aberta
 *   dentro do gerador (`reports/router.py:_stream_csv`), justamente para nunca
 *   carregar a competência inteira em memória. Um `fetch` + `blob` traria o
 *   arquivo todo para a memória da aba e desfaria esse cuidado.
 *
 * O arquivo contém **nome de paciente** e fica salvo na máquina de quem baixou:
 * quem oferecer este link precisa dizer isso ao lado dele.
 */
export function urlRelatorioConferenciaCsv(baseUrl: string, filtros: FiltrosConferencia): string {
  return `${baseUrl}${CAMINHO_CONFERENCIA}.csv${queryString(parametrosDaConferencia(filtros))}`;
}

/**
 * `GET /api/relatorios/metricas`: os dois blocos por competência.
 *
 * **Exige coordenador ou gestor** (`exigir_papel` no endpoint): métrica agregada
 * é leitura de gestão, não de conferência. Para a conferente isto responde 403,
 * e quem chama precisa tratar — não é falha, é a matriz do ADR 0001. Sem janela
 * informada a API devolve as 12 competências mais recentes.
 *
 * Ver {@link MetricasCompetencia} para a regra que não pode ser quebrada na
 * apresentação: `sistema` e `glosa_informada` nunca se fundem, e
 * `glosa_informada: null` é "ninguém informou", nunca zero.
 */
export async function metricas(
  baseUrl: string,
  params: MetricasParams = {},
  opcoes?: OpcoesRequisicao,
): Promise<MetricasResponse> {
  const response = await requisitar(
    baseUrl,
    `/api/relatorios/metricas${queryString({
      competencia_inicio: params.competencia_inicio,
      competencia_fim: params.competencia_fim,
      operadora_id: params.operadora_id,
    })}`,
    { method: "GET", cache: "no-store" },
    opcoes,
  );
  return (await response.json()) as MetricasResponse;
}

/**
 * `GET /api/relatorios/baseline`: os baselines de glosa já registrados.
 *
 * **Exige coordenador ou gestor**, como {@link metricas} — quem escreve é só o
 * gestor ({@link registrarBaseline}). Sem paginação e sem envelope: é um
 * cadastro pequeno, uma linha por competência/operadora, e devolve o array
 * direto.
 */
export async function listarBaselines(
  baseUrl: string,
  opcoes?: OpcoesRequisicao,
): Promise<BaselineOut[]> {
  const response = await requisitar(
    baseUrl,
    "/api/relatorios/baseline",
    { method: "GET", cache: "no-store" },
    opcoes,
  );
  return (await response.json()) as BaselineOut[];
}

/**
 * `PUT /api/relatorios/baseline`: registra ou corrige o baseline de glosa de
 * uma competência.
 *
 * **Exige o papel `gestor`, e aqui a autorização é o inverso da fila de
 * pendências**: em `PATCH /api/pendencias/{id}` o gestor é justamente quem não
 * pode agir; aqui ele é o único que pode. O baseline é a régua contra a qual o
 * próprio sistema é medido, e quem opera a conferência não mexe na régua que a
 * mede (ADR 0001). Esconder o formulário para os outros papéis é ergonomia; a
 * autoridade é o **403** desta chamada, e a tela precisa continuar de pé quando
 * ele chega.
 *
 * **É upsert pela chave natural `(competencia, operadora_id)`**: um `PUT` numa
 * competência que já tem baseline **substitui** os valores, sem aviso da API.
 * Não existe `DELETE` — corrigir é gravar de novo.
 *
 * 422 tem duas famílias que chegam diferentes: `documentos_glosados` maior que
 * `documentos_enviados` (e demais validações de corpo) vem como "parâmetros
 * inválidos" com a frase real em `detalhes` — use `detalhesDeValidacao`; já
 * "operadora não encontrada" vem pronta em `message`.
 */
export async function registrarBaseline(
  baseUrl: string,
  corpo: BaselineUpsert,
  opcoes?: OpcoesRequisicao,
): Promise<BaselineOut> {
  const response = await requisitar(
    baseUrl,
    "/api/relatorios/baseline",
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(corpo),
    },
    opcoes,
  );
  return (await response.json()) as BaselineOut;
}
