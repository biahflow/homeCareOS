/**
 * URL base das chamadas à API feitas pelo código que roda no navegador.
 *
 * Vazia de propósito. Com o proxy do Next (ADR 0002) o navegador fala apenas
 * com a origem do próprio app, então a base correta é a origem atual — e `""`
 * é o que faz o cliente montar caminho relativo (`/api/documentos`), que o
 * `apps/web/proxy.ts` repassa para a API.
 *
 * A URL real da API vive só no servidor (`API_URL`, sem `NEXT_PUBLIC_`): se
 * ela voltasse para cá seria inlinada no bundle e exposta ao navegador,
 * desfazendo o proxy.
 */
export const API_BASE_URL = "";
