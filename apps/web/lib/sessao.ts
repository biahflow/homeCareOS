import { cache } from "react";
import { ApiError, ehUsuario, obterUsuarioAtual } from "@homecareos/contracts";
import type { UsuarioOut } from "@homecareos/contracts";
import { apiUrl, opcoesAutenticadas } from "@/lib/api-servidor";

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
  const opcoes = await opcoesAutenticadas();
  if (opcoes.cookie === "") {
    // Sem cookie nenhum não há o que perguntar à API.
    return null;
  }

  let resposta;
  try {
    resposta = await obterUsuarioAtual(apiUrl(), opcoes);
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
