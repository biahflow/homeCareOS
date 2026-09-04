"use client";

import { useRouter } from "next/navigation";
import { useId, useOptimistic, useTransition } from "react";
import type { DocumentoStatus, Operadora } from "@homecareos/contracts";
import { CAMINHO_DOCUMENTOS, temFiltro, urlComFiltros } from "./filtros";
import type { FiltrosDeDocumentos } from "./filtros";
import { ROTULO_DE_STATUS_DOCUMENTO, STATUS_DE_DOCUMENTO } from "./vocabulario";

/**
 * Os controles de filtro da listagem de documentos.
 *
 * Client Component porque precisa reagir ao `change`, mas **não guarda o
 * filtro**: o valor exibido vem sempre da URL, por props, e cada mudança navega.
 * Manter uma cópia em `useState` criaria duas verdades — a da tela e a do
 * endereço — que divergem no primeiro "voltar" do navegador.
 *
 * Os valores atuais chegam por props em vez de `useSearchParams()` pelo mesmo
 * motivo das outras telas: quem já leu a URL é o Server Component que renderizou
 * a lista, e ler de novo aqui abriria espaço para a lista mostrar um filtro e o
 * controle mostrar outro.
 */
export function FiltrosDocumentos({
  filtros,
  operadoras,
}: {
  filtros: FiltrosDeDocumentos;
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
  const competenciaId = useId();
  const statusId = useId();
  const operadoraId = useId();

  function navegar(mudanca: Partial<FiltrosDeDocumentos>) {
    // `offset: 0` em toda mudança de filtro: manter a página 3 ao estreitar o
    // filtro leva a pessoa a uma lista vazia que existe só porque ela estava
    // adiante do fim do novo resultado.
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
                router.push(CAMINHO_DOCUMENTOS);
              })
            }
          >
            Limpar filtros
          </button>
        )}
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <div className="grid gap-1.5">
          <label htmlFor={competenciaId} className="form-label">
            Competência
          </label>
          <input
            id={competenciaId}
            type="month"
            className="field"
            value={exibidos.competencia ?? ""}
            disabled={navegando}
            onChange={(evento) => navegar({ competencia: evento.target.value || undefined })}
          />
        </div>

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
                status: (evento.target.value || undefined) as DocumentoStatus | undefined,
              })
            }
          >
            <option value="">Todos</option>
            {STATUS_DE_DOCUMENTO.map((status) => (
              <option key={status} value={status}>
                {ROTULO_DE_STATUS_DOCUMENTO[status]}
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
          <p className="text-xs text-muted">
            Documento sem operadora vinculada não aparece em nenhum filtro de operadora.
          </p>
        </div>
      </div>
    </section>
  );
}
