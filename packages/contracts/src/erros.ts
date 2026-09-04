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

/**
 * Prefixo que o Pydantic v2 coloca na frente da mensagem de um `ValueError`
 * levantado por um `model_validator`. É artefato de serialização, não texto que
 * a API escreveu — exibi-lo faria a pessoa ler "Value error," antes da frase que
 * de fato explica o que ela digitou errado.
 */
const PREFIXO_VALUE_ERROR = /^Value error,\s*/;

function mensagemDoDetalhe(item: unknown): string | undefined {
  if (typeof item !== "object" || item === null || !("msg" in item)) {
    return undefined;
  }
  const msg = (item as { msg?: unknown }).msg;
  return typeof msg === "string" ? msg.replace(PREFIXO_VALUE_ERROR, "") : undefined;
}

/**
 * As mensagens de validação que a API escreveu, tiradas de `error.detalhes`.
 *
 * Existe porque o 422 de validação de corpo tem a mensagem útil **no lugar
 * errado para quem só olha `message`**: `api/errors.py` responde a um
 * `RequestValidationError` com `mensagem: "parâmetros inválidos"` fixo e joga o
 * que a regra realmente disse (`"documentos_glosados não pode ser maior que
 * documentos_enviados"`, por exemplo) em `detalhes`, no formato do Pydantic.
 * Mostrar só `erro.message` entrega "parâmetros inválidos" a quem precisa saber
 * qual parâmetro e por quê.
 *
 * Devolve `[]` quando não há detalhe legível — e aí quem chama continua com
 * `erro.message`, que é o certo para 403, 404, 409 e para o 422 de
 * `HTTPException` (esse já traz a frase pronta em `mensagem`).
 *
 * Não classifica causa nem reordena: a regra é da API, e o texto sai como ela o
 * escreveu.
 */
export function detalhesDeValidacao(erro: ApiError): string[] {
  const corpo = erro.detail;
  if (typeof corpo !== "object" || corpo === null || !("error" in corpo)) {
    return [];
  }
  const envelope = (corpo as { error?: unknown }).error;
  if (typeof envelope !== "object" || envelope === null || !("detalhes" in envelope)) {
    return [];
  }
  const detalhes = (envelope as { detalhes?: unknown }).detalhes;
  if (!Array.isArray(detalhes)) {
    return [];
  }
  return detalhes
    .map(mensagemDoDetalhe)
    .filter((mensagem): mensagem is string => mensagem !== undefined && mensagem !== "");
}
