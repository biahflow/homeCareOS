"use client";

import { useRouter } from "next/navigation";
import { useId, useOptimistic, useTransition } from "react";
import { CAMINHO_USUARIOS, temFiltro, urlComFiltros } from "./filtros";
import type { FiltrosDeUsuarios } from "./filtros";

/**
 * O valor do `<select>` de situação, que é `string` — a URL e o DOM não têm
 * booleano. `""` é "todos", e é o que faz o terceiro estado do filtro caber num
 * controle de dois valores.
 */
function valorDaSituacao(ativo: boolean | undefined): string {
  return ativo === undefined ? "" : String(ativo);
}

function situacaoDoValor(valor: string): boolean | undefined {
  return valor === "" ? undefined : valor === "true";
}

/**
 * O controle de filtro da administração de usuários.
 *
 * Client Component porque precisa reagir ao `change`, mas **não guarda o
 * filtro**: o valor exibido vem sempre da URL, por props, e cada mudança navega.
 * Manter uma cópia em `useState` criaria duas verdades — a da tela e a do
 * endereço — que divergem no primeiro "voltar" do navegador.
 *
 * Os valores atuais chegam por props em vez de `useSearchParams()` de propósito:
 * quem já leu a URL é o Server Component que renderizou a lista, e ler de novo
 * aqui abriria espaço para a lista mostrar um filtro e o controle mostrar outro.
 */
export function FiltrosUsuarios({ filtros }: { filtros: FiltrosDeUsuarios }) {
  const router = useRouter();
  const [navegando, iniciarNavegacao] = useTransition();
  // O que o controle mostra enquanto a navegação não terminou. Sem isto o campo
  // volta ao valor antigo no instante seguinte ao clique — a resposta do
  // servidor é que traz o novo — e a pessoa vê a própria escolha ser desfeita.
  // Não é uma segunda fonte de verdade: `useOptimistic` descarta o valor
  // provisório assim que a URL chega pelas props.
  const [exibidos, exibirEscolha] = useOptimistic(filtros);
  const situacaoId = useId();

  function navegar(mudanca: Partial<FiltrosDeUsuarios>) {
    // `offset: 0` em toda mudança de filtro, e não é detalhe: manter a página 3
    // ao estreitar o filtro leva a pessoa a uma lista vazia que existe só
    // porque ela estava adiante do fim do novo resultado.
    const proximos = { ...filtros, ...mudanca, offset: 0 };
    iniciarNavegacao(() => {
      exibirEscolha(proximos);
      router.push(urlComFiltros(proximos));
    });
  }

  return (
    <section className="panel" aria-busy={navegando}>
      <div className="panel-heading">
        <h2>Filtros</h2>
        {temFiltro(exibidos) && (
          <button
            type="button"
            className="btn btn--ghost h-8 min-h-8 px-3 text-xs"
            disabled={navegando}
            onClick={() =>
              iniciarNavegacao(() => {
                exibirEscolha({ offset: 0 });
                router.push(CAMINHO_USUARIOS);
              })
            }
          >
            Limpar filtros
          </button>
        )}
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <div className="grid gap-1.5">
          <label htmlFor={situacaoId} className="form-label">
            Situação
          </label>
          <select
            id={situacaoId}
            className="field"
            value={valorDaSituacao(exibidos.ativo)}
            disabled={navegando}
            onChange={(evento) => navegar({ ativo: situacaoDoValor(evento.target.value) })}
          >
            <option value="">Todos</option>
            <option value="true">Somente ativos</option>
            <option value="false">Somente desativados</option>
          </select>
          <p className="text-xs text-muted">
            Contas desativadas não somem do cadastro: elas continuam respondendo por quem fez o quê
            no histórico de conferência.
          </p>
        </div>
      </div>
    </section>
  );
}
