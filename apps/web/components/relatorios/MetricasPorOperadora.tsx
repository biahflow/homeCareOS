import type { MetricasOperadora } from "@homecareos/contracts";
import { formatarInteiro, formatarPercentual } from "./formatos";

/**
 * Quanto trabalho cada operadora dá, na mesma janela de competências dos
 * cartões acima (`reports/schema.MetricasOperadora`).
 *
 * A linha com `operadora_id: null` agrupa os documentos que **ninguém
 * conseguiu vincular a uma operadora** — o comentário do schema da API é
 * explícito: é exatamente o caso que mais interessa olhar. Por isso ela nunca
 * é filtrada desta lista, e ganha um selo em vez de passar despercebida entre
 * as demais.
 */
export function MetricasPorOperadora({ porOperadora }: { porOperadora: MetricasOperadora[] }) {
  if (porOperadora.length === 0) {
    return (
      <p className="empty-state">
        Nenhum documento nesta janela de competências. A distribuição por operadora nasce dos
        documentos recebidos — não há como calculá-la sem eles.
      </p>
    );
  }

  return (
    <ul className="m-0 flex list-none flex-col divide-y divide-line p-0">
      {porOperadora.map((operadora) => (
        <li
          key={operadora.operadora_id ?? "sem-operadora"}
          className="grid gap-1 py-3 first:pt-0 last:pb-0"
        >
          <p className="m-0 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
            {operadora.operadora_id === null ? (
              // Selo, não texto corrido: é a linha que mais precisa ser vista,
              // e não só lida.
              <span className="state state--2">Sem operadora vinculada</span>
            ) : (
              <strong className="text-ink first-letter:uppercase">{operadora.nome}</strong>
            )}
          </p>
          <p className="m-0 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
            <span>
              {formatarInteiro(operadora.documentos)}{" "}
              {operadora.documentos === 1 ? "documento" : "documentos"}
            </span>
            <span>
              {formatarInteiro(operadora.documentos_com_pendencia)} com pendência (
              {formatarPercentual(operadora.taxa_documentos_com_pendencia)})
            </span>
          </p>
        </li>
      ))}
    </ul>
  );
}
