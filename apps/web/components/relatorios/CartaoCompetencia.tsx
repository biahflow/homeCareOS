import type { MetricasCompetencia } from "@homecareos/contracts";
import {
  formatarCompetencia,
  formatarHoras,
  formatarInteiro,
  formatarPercentual,
  formatarReais,
} from "./formatos";

/**
 * Uma competência com os seus **dois blocos, lado a lado e nomeados**.
 *
 * A separação é o contrato, não a estética. `sistema` mede o que a conferência
 * pegou **antes** do envio à operadora; `glosa_informada` mede o que a operadora
 * recusou **depois**, digitado de um demonstrativo. São medidas de origens
 * diferentes: este componente não soma os dois, não divide um pelo outro e não
 * deriva deles nenhum indicador único de "eficácia" — cruzá-las é decisão de
 * produto que ninguém tomou, e o número resultante pareceria medido quando seria
 * inventado.
 *
 * Cada bloco diz de onde vem, inclusive a `fonte` do baseline — que a API exige
 * (`min_length=1`) exatamente para isso.
 */

function Medida({
  rotulo,
  valor,
  detalhe,
  alerta = false,
}: {
  rotulo: string;
  valor: string;
  detalhe?: string;
  alerta?: boolean;
}) {
  return (
    <div>
      <dt className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">{rotulo}</dt>
      <dd
        className={`m-0 text-lg font-semibold tracking-[-0.02em] ${
          alerta ? "text-danger" : "text-ink"
        }`}
      >
        {valor}
      </dd>
      {detalhe !== undefined && <p className="m-0 text-xs text-muted">{detalhe}</p>}
    </div>
  );
}

export function CartaoCompetencia({
  metricas,
  podeRegistrarBaseline,
}: {
  metricas: MetricasCompetencia;
  /** Só muda o texto que diz a quem cabe registrar o baseline que falta. */
  podeRegistrarBaseline: boolean;
}) {
  const { sistema, glosa_informada: glosa } = metricas;

  return (
    <article className="rounded-xl border border-line bg-canvas p-4">
      <header className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="m-0 text-sm font-semibold tracking-[-0.01em] text-ink first-letter:uppercase">
          {formatarCompetencia(metricas.competencia)}
        </h3>
        <code className="text-[11px] text-muted">{metricas.competencia}</code>
      </header>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* Bloco 1 — o que a conferência mediu, antes do envio. */}
        <section className="rounded-xl border border-line bg-white p-4">
          <h4 className="m-0 text-sm font-semibold text-ink">Conferência (medido pelo sistema)</h4>
          <p className="mt-1 mb-3 text-xs leading-5 text-muted">
            Pendência detectada <strong>antes</strong> do envio à operadora, apurada dos documentos
            desta competência.
          </p>

          <dl className="m-0 grid grid-cols-2 gap-3">
            <Medida rotulo="Documentos" valor={formatarInteiro(sistema.documentos)} />
            <Medida
              rotulo="Com pendência"
              valor={formatarInteiro(sistema.documentos_com_pendencia)}
              detalhe={`${formatarPercentual(sistema.taxa_documentos_com_pendencia)} dos documentos`}
            />
            <Medida rotulo="Pendências abertas" valor={formatarInteiro(sistema.pendencias_abertas)} />
            <Medida
              rotulo="Vencidas"
              valor={formatarInteiro(sistema.pendencias_vencidas)}
              detalhe={`${formatarInteiro(sistema.pendencias_proximos_7_dias)} vencem em 7 dias`}
              alerta={sistema.pendencias_vencidas > 0}
            />
            <Medida
              rotulo="Tempo médio de resolução"
              // `null` é "nenhuma pendência resolvida ainda". Zero diria que a
              // operação resolve instantaneamente — o oposto do que o dado diz.
              valor={
                sistema.tempo_medio_resolucao_horas === null
                  ? "—"
                  : formatarHoras(sistema.tempo_medio_resolucao_horas)
              }
              detalhe={
                sistema.tempo_medio_resolucao_horas === null
                  ? "Nenhuma pendência resolvida ainda"
                  : "Das pendências já resolvidas"
              }
            />
          </dl>
        </section>

        {/* Bloco 2 — o que a operadora recusou, depois do envio. Nunca somado
            nem cruzado com o bloco acima. */}
        <section className="rounded-xl border border-line bg-white p-4">
          <h4 className="m-0 text-sm font-semibold text-ink">Glosa informada (digitada à mão)</h4>
          <p className="mt-1 mb-3 text-xs leading-5 text-muted">
            O que a operadora recusou <strong>depois</strong> do envio, lido de um demonstrativo.
            Não é medido pelo sistema.
          </p>

          {glosa === null ? (
            // `null` significa "ninguém informou", e nunca zero: mostrar 0% de
            // glosa aqui afirmaria que a conferência zerou a glosa — o número
            // que justifica o produto, inventado pela tela.
            <div className="grid gap-2">
              <p className="empty-state m-0">
                <strong className="text-ink">Não informado.</strong> Nenhum baseline de glosa foi
                registrado para esta competência — e ausência de informação não é glosa zero.
              </p>
              <p className="m-0 text-xs text-muted">
                {podeRegistrarBaseline
                  ? "Registre em “Baseline de glosa”, abaixo, a partir do demonstrativo da operadora."
                  : "Quem registra o baseline é o gestor, a partir do demonstrativo da operadora."}
              </p>
            </div>
          ) : (
            <>
              <dl className="m-0 grid grid-cols-2 gap-3">
                <Medida rotulo="Enviados" valor={formatarInteiro(glosa.documentos_enviados)} />
                <Medida
                  rotulo="Glosados"
                  valor={formatarInteiro(glosa.documentos_glosados)}
                  detalhe={`${formatarPercentual(glosa.taxa_glosa)} dos enviados`}
                />
                <Medida
                  rotulo="Valor glosado"
                  // Inteiro em centavos no contrato; reais só aqui, na tela.
                  valor={
                    glosa.valor_glosado_centavos === null
                      ? "—"
                      : formatarReais(glosa.valor_glosado_centavos)
                  }
                  detalhe={glosa.valor_glosado_centavos === null ? "Não informado" : undefined}
                />
                <Medida
                  rotulo="Horas de conferência"
                  valor={
                    glosa.horas_conferencia === null ? "—" : formatarHoras(glosa.horas_conferencia)
                  }
                  detalhe={glosa.horas_conferencia === null ? "Não informado" : undefined}
                />
              </dl>
              <p className="mt-3 mb-0 text-xs text-muted">
                Fonte: <strong className="text-ink">{glosa.fonte}</strong>
              </p>
            </>
          )}
        </section>
      </div>
    </article>
  );
}
