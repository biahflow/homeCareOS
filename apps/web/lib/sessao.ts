import { cookies } from "next/headers";
import { cache } from "react";
import { ApiError, ehUsuario, obterUsuarioAtual } from "@homecareos/contracts";
import type { UsuarioOut } from "@homecareos/contracts";

/**
 * URL da API para chamadas **de servidor**, lida em runtime.
 *
 * O mesmo valor e o mesmo default de `proxy.ts` — e pelo mesmo motivo (ADR
 * 0002): `API_URL` é variável de servidor, nunca `NEXT_PUBLIC_`, senão o Next
 * a inlina no bundle e o navegador passa a conhecer a API.
 *
 * Aqui não passamos pelo proxy de propósito: ele existe para o **navegador**
 * falar com a origem do Next. Este código já está no servidor do Next — usá-lo
 * seria um salto de rede a mais para sair e voltar ao mesmo processo.
 */
function apiUrl(): string {
  return process.env.API_URL ?? "http://localhost:8001";
}

/**
 * O usuário desta requisição, ou `null` quando não há sessão que valha.
 *
 * Só roda no servidor: depende de `cookies()`, de `next/headers`. Um Client
 * Component que importe isto quebra no build — que é o comportamento desejado,
 * porque o cookie de sessão é `httpOnly` e o navegador não tem como lê-lo.
 *
 * **`null` também é a resposta para sessão pendente de MFA.** `GET /api/auth/eu`
 * devolve 401 para quem só apresentou a senha, exatamente como para quem não
 * tem sessão nenhuma (`auth/sessoes.py:97`) — e é assim que tem que ser: até o
 * segundo fator, não existe usuário para esta aplicação desenhar. Quem está no
 * meio do login vai para `/login`, digita de novo e cai em `/mfa`; inventar um
 * estado no cliente para "lembrar" que faltava o segundo fator seria criar uma
 * segunda fonte de verdade que pode divergir da API.
 *
 * `cache` do React memoiza o resultado dentro de **uma** renderização: dois
 * componentes que perguntem quem está logado fazem uma chamada só, e não duas
 * respostas possivelmente diferentes na mesma tela.
 */
export const usuarioDaSessao = cache(async (): Promise<UsuarioOut | null> => {
  const cookieStore = await cookies();
  const cookie = cookieStore.toString();
  if (cookie === "") {
    // Sem cookie nenhum não há o que perguntar à API.
    return null;
  }

  let resposta;
  try {
    // O header `Cookie` inteiro, e não só o cookie de sessão pelo nome: o nome
    // é configurável na API (`SESSAO_COOKIE_NOME`) e repeti-lo aqui criaria uma
    // cópia para envelhecer. É também exatamente o que o navegador já manda
    // para a API em toda chamada que passa pelo proxy.
    resposta = await obterUsuarioAtual(apiUrl(), { cookie });
  } catch (erro) {
    if (erro instanceof ApiError && erro.status === 401) {
      return null;
    }
    // API fora do ar ou com defeito não é "você não está logado": mandar para
    // a tela de login esconderia a causa e faria a pessoa culpar a própria
    // senha. O erro sobe.
    throw erro;
  }

  // `X-API-Key` não é pessoa: a área logada não tem nome, papel nem histórico
  // para atribuir a ela. Nenhum navegador chega aqui assim, mas o contrato de
  // `/eu` permite, e o tipo obriga a decidir.
  return ehUsuario(resposta) ? resposta : null;
});
