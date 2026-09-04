import type { VolumeDia } from "@homecareos/contracts";
import { formatarDataIso, formatarInteiro } from "./formatos";

/**
 * Documentos recebidos por dia, na mesma janela de competências dos cartões
 * acima — "para enxergar o pico do fechamento" (`reports/schema.VolumeDia`).
 *
 * O schema pede um gráfico, mas adicionar biblioteca de visualização é
 * decisão de produto que ninguém tomou nesta fatia. A barra é CSS puro: a
 * largura de cada trilha é a proporção do dia sobre o **maior** valor da
 * janela, sem nenhuma dependência nova.
 *
 * A ordem é a que a API devolve — crescente por dia — porque é o que faz um
 * pico ser lido como pico: a lista se lê como uma linha do tempo, não como um
 * ranking.
 */
export function VolumePorDia({ dias }: { dias: VolumeDia[] }) {
  if (dias.length === 0) {
    return (
      <p className="empty-state">
        Nenhum documento nesta janela de competências. O volume diário nasce dos documentos
        recebidos — não há como calculá-lo sem eles.
      </p>
    );
  }

  const maximo = Math.max(...dias.map((dia) => dia.documentos), 1);

  return (
    // Altura limitada e rolagem própria: uma janela sem competência escolhida
    // cobre até 12 meses, e uma lista de ~360 dias não pode empurrar o resto
    // da tela para baixo.
    <ul className="m-0 flex max-h-72 list-none flex-col gap-2 overflow-y-auto p-0 pr-1">
      {dias.map((dia) => (
        <li
          key={dia.data}
          className="grid grid-cols-[5.5rem_1fr_3.5rem] items-center gap-3 text-xs"
        >
          <span className="text-muted">{formatarDataIso(dia.data)}</span>
          <span className="h-2 overflow-hidden rounded-full bg-line" aria-hidden="true">
            <span
              className="block h-2 rounded-full bg-brand-500"
              style={{ width: `${(dia.documentos / maximo) * 100}%` }}
            />
          </span>
          <span className="text-right font-semibold text-ink">
            {formatarInteiro(dia.documentos)}
          </span>
        </li>
      ))}
    </ul>
  );
}
