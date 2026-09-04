import { ArrowDown, ArrowUp, Minus } from "lucide-react";
import type { ComparacaoGlosa, MetricasCompetencia } from "@homecareos/contracts";
import {
  formatarCompetencia,
  formatarInteiro,
  formatarPercentual,
  formatarPontosPercentuais,
} from "./formatos";

/**
 * Antes/depois honesto: a **mesma** medida — glosa informada — nas duas
 * pontas da janela (`reports/schema.ComparacaoGlosa`). Nunca ao lado do bloco
 * "medido pelo sistema" de um jeito que sugira que um virou o outro; é
 * `taxa_glosa` comparada com `taxa_glosa`, em duas competências.
 *
 * Duas armadilhas do contrato, tratadas aqui:
 *
 * - **`null` é ausência, não zero.** Enquanto menos de duas competências da
 *   janela tiverem baseline, não há ponta para comparar. Renderizar "0 p.p."
 *   ou "sem variação" afirmaria que a conferência não mudou nada — a
 *   conclusão que o produto existe para permitir ou refutar.
 * - **`variacao_pontos_percentuais` negativa é melhora** (a glosa caiu). O
 *   número nunca é invertido; o sinal vira ícone e palavra ("queda de X
 *   p.p.") para que ninguém o leia ao contrário numa reunião.
 */
export function PainelComparacaoGlosa({
  comparacao,
  competencias,
}: {
  comparacao: ComparacaoGlosa | null;
  /** As competências já carregadas nesta página — só para contar quantas têm baseline; nenhuma chamada nova. */
  competencias: MetricasCompetencia[];
}) {
  if (comparacao === null) {
    const comBaseline = competencias.filter(
      (competencia) => competencia.glosa_informada !== null,
    ).length;

    return (
      <div className="grid gap-2">
        <p className="empty-state m-0">
          <strong className="text-ink">Ainda não há comparação.</strong>{" "}
          {comBaseline === 0
            ? "Nenhuma competência desta janela tem baseline de glosa registrado."
            : `Só ${formatarInteiro(comBaseline)} ${
                comBaseline === 1 ? "competência" : "competências"
              } desta janela ${comBaseline === 1 ? "tem" : "têm"} baseline de glosa registrado.`}
        </p>
        <p className="m-0 text-xs text-muted">
          Registre baseline em pelo menos duas competências desta janela, em “Baseline de glosa”
          logo abaixo, para habilitar o antes/depois.
        </p>
      </div>
    );
  }

  const variacao = comparacao.variacao_pontos_percentuais;
  const pontos = formatarPontosPercentuais(variacao);
  // Negativa é melhora (a glosa caiu); positiva é piora; zero aqui é uma
  // leitura real (as duas taxas empataram) — diferente do `null` acima, que é
  // ausência de leitura.
  const tendencia =
    variacao < 0
      ? { Icone: ArrowDown, texto: `Queda de ${pontos}`, classe: "text-emerald-700" }
      : variacao > 0
        ? { Icone: ArrowUp, texto: `Alta de ${pontos}`, classe: "text-danger" }
        : { Icone: Minus, texto: "Sem variação", classe: "text-muted" };

  return (
    <div className="grid gap-3">
      <p className="m-0 flex flex-wrap items-baseline gap-x-2 gap-y-1 text-sm">
        <strong className="text-ink first-letter:uppercase">
          {formatarCompetencia(comparacao.competencia_inicial)}
        </strong>
        <span className="text-muted">→</span>
        <strong className="text-ink first-letter:uppercase">
          {formatarCompetencia(comparacao.competencia_final)}
        </strong>
      </p>

      <div className="flex flex-wrap items-center gap-4">
        <span className="text-xs text-muted">
          {formatarPercentual(comparacao.taxa_glosa_inicial)} →{" "}
          {formatarPercentual(comparacao.taxa_glosa_final)}
        </span>
        <span className={`flex items-center gap-1.5 text-sm font-semibold ${tendencia.classe}`}>
          <tendencia.Icone className="size-4" aria-hidden="true" />
          {tendencia.texto}
        </span>
      </div>

      <p className="m-0 text-xs text-muted">
        Queda de glosa é melhora — a operadora recusou proporcionalmente menos documentos na
        competência final do que na inicial. A comparação é entre glosa informada nas duas pontas,
        nunca entre o que a operadora recusou e o que o sistema mediu.
      </p>
    </div>
  );
}
