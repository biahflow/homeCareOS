/**
 * Erro tipado para chamadas HTTP contra a API. Nunca lançamos string solta:
 * o chamador precisa de `status` para decidir o que mostrar ao usuário.
 */
export interface UploadErrorPayload {
  /** Código HTTP retornado pela API, ou 0 quando a requisição não chegou a sair. */
  status: number;
  /** Mensagem pronta para exibição ao usuário. */
  message: string;
  /** Corpo bruto da resposta de erro, quando disponível, para diagnóstico. */
  detail?: unknown;
}

export class UploadError extends Error {
  readonly status: number;
  readonly detail?: unknown;

  constructor(payload: UploadErrorPayload) {
    super(payload.message);
    this.name = "UploadError";
    this.status = payload.status;
    this.detail = payload.detail;
  }
}
