"use client";

import { useRouter } from "next/navigation";
import { useId, useOptimistic, useTransition } from "react";
import type { StatusAlerta, TipoAlerta } from "@homecareos/contracts";
import {
  CAMINHO_ALERTAS,
  ROTULO_DE_STATUS,
  ROTULO_DE_TIPO,
  STATUS_DE_ALERTA,
  TIPOS_DE_ALERTA,
  temFiltro,
  urlComFiltros,
} from "./filtros";
import type { FiltrosDeAlertas } from "./filtros";

/**
 * Os controles de filtro do log de alertas.
 *
 * Client Component porque precisa reagir ao `change`, mas **não guarda o
 * filtro**: o valor exibido vem sempre da URL, por props, e cada mudança
 * navega. Manter uma cópia em `useState` criaria duas verdades — a da tela e a
 * do endereço — que divergem no primeiro "voltar" do navegador.
 *
 * Os valores atuais chegam por props em vez de `useSearchParams()` de
 * propósito: quem já leu a URL é o Server Component que renderizou a lista, e
 * ler de novo aqui abriria espaço para a lista mostrar um filtro e o controle
 * mostrar outro.
 *
 * Só `tipo` e `status` têm controle aqui. `documento_id` é filtro de URL (ver
 * `filtros.ts`), sem seletor nesta tela.
 */
export function FiltrosAlertas({ filtros }: { filtros: FiltrosDeAlertas }) {
  const router = useRouter();
  const [navegando, iniciarNavegacao] = useTransition();
  // O que os controles mostram enquanto a navegação não terminou. Sem isto o
  // campo volta ao valor antigo no instante seguinte ao clique — a resposta do
  // servidor é que traz o novo — e a pessoa vê a própria escolha ser desfeita.
  // Não é uma segunda fonte de verdade: `useOptimistic` descarta o valor
  // provisório assim que a URL chega pelas props.
  const [exibidos, exibirEscolha] = useOptimistic(filtros);
  const tipoId = useId();
  const statusId = useId();

  function navegar(mudanca: Partial<FiltrosDeAlertas>) {
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
                router.push(CAMINHO_ALERTAS);
              })
            }
          >
            Limpar filtros
          </button>
        )}
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="grid gap-1.5">
          <label htmlFor={tipoId} className="form-label">
            Tipo
          </label>
          <select
            id={tipoId}
            className="field"
            value={exibidos.tipo ?? ""}
            disabled={navegando}
            onChange={(evento) =>
              navegar({ tipo: (evento.target.value || undefined) as TipoAlerta | undefined })
            }
          >
            <option value="">Todos</option>
            {TIPOS_DE_ALERTA.map((tipo) => (
              <option key={tipo} value={tipo}>
                {ROTULO_DE_TIPO[tipo]}
              </option>
            ))}
          </select>
        </div>

        <div className="grid gap-1.5">
          <label htmlFor={statusId} className="form-label">
            Situação
          </label>
          <select
            id={statusId}
            className="field"
            value={exibidos.status ?? ""}
            disabled={navegando}
            onChange={(evento) =>
              navegar({
                status: (evento.target.value || undefined) as StatusAlerta | undefined,
              })
            }
          >
            <option value="">Todas</option>
            {STATUS_DE_ALERTA.map((status) => (
              <option key={status} value={status}>
                {ROTULO_DE_STATUS[status]}
              </option>
            ))}
          </select>
        </div>
      </div>
    </section>
  );
}
