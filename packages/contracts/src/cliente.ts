import { ApiError } from "./erros";
import type {
  EuResposta,
  LoginParams,
  LoginResposta,
  MfaVerificarParams,
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
