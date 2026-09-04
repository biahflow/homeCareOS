import { UploadError } from "./erros";
import type { UploadParams, UploadResponse } from "./tipos";

function extrairMensagem(detail: unknown, status: number): string {
  if (
    typeof detail === "object" &&
    detail !== null &&
    "message" in detail &&
    typeof (detail as { message?: unknown }).message === "string"
  ) {
    return (detail as { message: string }).message;
  }
  if (
    typeof detail === "object" &&
    detail !== null &&
    "detail" in detail &&
    typeof (detail as { detail?: unknown }).detail === "string"
  ) {
    return (detail as { detail: string }).detail;
  }
  return `Falha no envio (HTTP ${status}).`;
}

/**
 * Envia um documento para a API como `multipart/form-data`, identificando a
 * tentativa pelo header `Idempotency-Key` para que reenvios do mesmo arquivo
 * não dupliquem documentos.
 *
 * Nunca lança string solta: erros de rede ou HTTP viram {@link UploadError}.
 */
export async function uploadDocumento(
  baseUrl: string,
  params: UploadParams,
): Promise<UploadResponse> {
  const formData = new FormData();
  formData.append("arquivo", params.arquivo);
  formData.append("competencia", params.competencia);

  let response: Response;
  try {
    response = await fetch(`${baseUrl}/api/documentos`, {
      method: "POST",
      headers: {
        "Idempotency-Key": params.idempotencyKey,
      },
      body: formData,
    });
  } catch (cause) {
    throw new UploadError({
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
    throw new UploadError({
      status: response.status,
      message: extrairMensagem(detail, response.status),
      detail,
    });
  }

  return (await response.json()) as UploadResponse;
}
