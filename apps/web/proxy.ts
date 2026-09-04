import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/**
 * Repassa `/api/*` para a API (ADR 0002).
 *
 * Isto existe como `proxy.ts` — o que até o Next 15 se chamava middleware — e
 * não como `rewrites` no `next.config.ts` por um motivo medido, não estético:
 * o `next build` serializa a `destination` dos rewrites em
 * `.next/routes-manifest.json`, congelando a URL da API na imagem. Uma imagem
 * construída em desenvolvimento carregava `http://localhost:8001` para dentro
 * do Compose e respondia 500 (ECONNREFUSED) — o mesmo defeito de classe do
 * `NEXT_PUBLIC_API_URL` que a decisão removeu, só que mais silencioso.
 *
 * Aqui `process.env.API_URL` é lido a cada requisição, em runtime, e a mesma
 * imagem serve qualquer ambiente.
 */
export function proxy(request: NextRequest) {
  const apiUrl = process.env.API_URL ?? "http://localhost:8001";
  const destino = new URL(
    request.nextUrl.pathname + request.nextUrl.search,
    apiUrl,
  );
  return NextResponse.rewrite(destino);
}

export const config = {
  matcher: "/api/:path*",
};
