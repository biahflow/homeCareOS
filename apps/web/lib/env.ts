/**
 * URL base da API. O default acompanha a porta publicada pelo docker-compose
 * (`API_PORT`, default 8001) — não a porta interna do container (8000).
 */
export const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8001";
