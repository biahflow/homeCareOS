/**
 * Erro tipado para chamadas HTTP contra a API. Nunca lançamos string solta:
 * o chamador precisa de `status` para decidir o que mostrar ao usuário.
 */
export interface ApiErrorPayload {
  /** Código HTTP retornado pela API, ou 0 quando a requisição não chegou a sair. */
  status: number;
  /** Mensagem pronta para exibição ao usuário. */
  message: string;
  /** Corpo bruto da resposta de erro, quando disponível, para diagnóstico. */
  detail?: unknown;
}

/**
 * Erro de qualquer chamada à API — não só do upload.
 *
 * O nome antigo (`UploadError`) nasceu quando o pacote tinha um endpoint só.
 * Ele é o mesmo erro para login, sessão e documentos: quem trata precisa de
 * `status` (401 é sessão, 429 é bloqueio, 0 é rede) muito mais do que da rota
 * que falhou.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail?: unknown;

  constructor(payload: ApiErrorPayload) {
    super(payload.message);
    this.name = "ApiError";
    this.status = payload.status;
    this.detail = payload.detail;
  }
}
