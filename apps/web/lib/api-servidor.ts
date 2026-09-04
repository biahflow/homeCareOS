import { cookies } from "next/headers";
import type { OpcoesRequisicao } from "@homecareos/contracts";

/**
 * Como o código **de servidor** do Next fala com a API.
 *
 * Só roda no servidor: `opcoesAutenticadas` depende de `cookies()`, de
 * `next/headers`. Um Client Component que importe este módulo quebra no build —
 * que é o comportamento desejado, porque o cookie de sessão é `httpOnly` e o
 * navegador não tem como lê-lo. O equivalente para o navegador é
 * `lib/env.ts:API_BASE_URL`, que é vazio de propósito (ADR 0002).
 */

/**
 * URL da API para chamadas de servidor, lida em runtime.
 *
 * O mesmo valor e o mesmo default de `proxy.ts` — e pelo mesmo motivo (ADR
 * 0002): `API_URL` é variável de servidor, nunca `NEXT_PUBLIC_`, senão o Next
 * a inlina no bundle e o navegador passa a conhecer a API.
 *
 * Aqui não passamos pelo proxy de propósito: ele existe para o **navegador**
 * falar com a origem do Next. Este código já está no servidor do Next — usá-lo
 * seria um salto de rede a mais para sair e voltar ao mesmo processo.
 */
export function apiUrl(): string {
  return process.env.API_URL ?? "http://localhost:8001";
}

/**
 * As opções que autenticam uma chamada de servidor à API: o header `Cookie`
 * desta requisição, repassado inteiro.
 *
 * Inteiro, e não só o cookie de sessão pelo nome: o nome é configurável na API
 * (`SESSAO_COOKIE_NOME`) e repeti-lo aqui criaria uma cópia para envelhecer. É
 * também exatamente o que o navegador já manda para a API em toda chamada que
 * passa pelo proxy.
 *
 * Chamar isto torna a rota dinâmica, o que é o correto: resposta que depende de
 * quem está logado não pode ser prerenderizada e servida a outra pessoa.
 */
export async function opcoesAutenticadas(): Promise<OpcoesRequisicao> {
  const cookieStore = await cookies();
  return { cookie: cookieStore.toString() };
}
