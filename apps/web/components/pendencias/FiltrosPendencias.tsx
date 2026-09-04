"use client";

import { useRouter } from "next/navigation";
import { useId, useOptimistic, useTransition } from "react";
import type { Operadora, PendenciaStatus } from "@homecareos/contracts";
import {
  CAMINHO_PENDENCIAS,
  ROTULO_DE_STATUS,
  STATUS_DE_PENDENCIA,
  temFiltro,
  urlComFiltros,
} from "./filtros";
import type { FiltrosDePendencias } from "./filtros";

/**
 * Os controles de filtro da fila.
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
 */
export function FiltrosPendencias({
  filtros,
  operadoras,
}: {
  filtros: FiltrosDePendencias;
  operadoras: Operadora[];
}) {
  const router = useRouter();
  const [navegando, iniciarNavegacao] = useTransition();
  // O que os controles mostram enquanto a navegação não terminou. Sem isto o
  // campo volta ao valor antigo no instante seguinte ao clique — a resposta do
  // servidor é que traz o novo — e a pessoa vê a própria escolha ser desfeita.
  // Não é uma segunda fonte de verdade: `useOptimistic` descarta o valor
  // provisório assim que a URL chega pelas props.
  const [exibidos, exibirEscolha] = useOptimistic(filtros);
  const statusId = useId();
  const operadoraId = useId();
  const deadlineId = useId();

  function navegar(mudanca: Partial<FiltrosDePendencias>) {
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
                router.push(CAMINHO_PENDENCIAS);
              })
            }
          >
            Limpar filtros
          </button>
        )}
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <div className="grid gap-1.5">
          <label htmlFor={statusId} className="form-label">
            Status
          </label>
          <select
            id={statusId}
            className="field"
            value={exibidos.status ?? ""}
            disabled={navegando}
            onChange={(evento) =>
              navegar({
                status: (evento.target.value || undefined) as PendenciaStatus | undefined,
              })
            }
          >
            <option value="">Todos</option>
            {STATUS_DE_PENDENCIA.map((status) => (
              <option key={status} value={status}>
                {ROTULO_DE_STATUS[status]}
              </option>
            ))}
          </select>
        </div>

        <div className="grid gap-1.5">
          <label htmlFor={operadoraId} className="form-label">
            Operadora
          </label>
          <select
            id={operadoraId}
            className="field"
            value={exibidos.operadora_id ?? ""}
            disabled={navegando}
            onChange={(evento) => navegar({ operadora_id: evento.target.value || undefined })}
          >
            <option value="">Todas</option>
            {operadoras.map((operadora) => (
              <option key={operadora.id} value={operadora.id}>
                {operadora.nome}
              </option>
            ))}
          </select>
        </div>

        <div className="grid gap-1.5">
          <label htmlFor={deadlineId} className="form-label">
            Prazo até
          </label>
          <input
            id={deadlineId}
            type="date"
            className="field"
            value={exibidos.deadline ?? ""}
            disabled={navegando}
            onChange={(evento) => navegar({ deadline: evento.target.value || undefined })}
          />
          <p className="text-xs text-muted">
            Inclui o dia inteiro da data escolhida.
          </p>
        </div>
      </div>
    </section>
  );
}
