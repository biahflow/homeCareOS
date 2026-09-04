import Link from "next/link";
import { redirect } from "next/navigation";
import { ApiError, listarOperadoras, listarPendencias, resumoPendencias } from "@homecareos/contracts";
import type {
  Operadora,
  PendenciaItem,
  RespostaPaginada,
  ResumoPendencias,
} from "@homecareos/contracts";
import { AcaoPendencia } from "@/components/pendencias/AcaoPendencia";
import { AvisosDaFila } from "@/components/pendencias/AvisosDaFila";
import {
  LIMITE_POR_PAGINA,
  ROTULO_DE_STATUS,
  STATUS_DE_PENDENCIA,
  VARIANTE_DE_STATUS,
  lerFiltros,
  temFiltro,
  urlComFiltros,
} from "@/components/pendencias/filtros";
import { FiltrosPendencias } from "@/components/pendencias/FiltrosPendencias";
import { formatarPrazo, marcadorDeVencimento } from "@/components/pendencias/prazo";
import { apiUrl, opcoesAutenticadas } from "@/lib/api-servidor";
import { usuarioDaSessao } from "@/lib/sessao";

/**
 * O documento é o que amarra as pendências entre si: a classificação
 * automática abre o mesmo `tipo_problema` em dezenas de documentos, e sem esta
 * referência duas linhas idênticas na tela são dois trabalhos diferentes que
 * ninguém distingue. Oito caracteres bastam para separar as da mesma tela; o id
 * inteiro fica no `title`, para copiar quando for preciso.
 */
function referenciaDoDocumento(documentoId: string): string {
  return documentoId.slice(0, 8);
}

/** Como a pendência é nomeada na confirmação de resolver e nos avisos. */
function nomeDaPendencia(pendencia: PendenciaItem): string {
  const alvo =
    pendencia.campo === null
      ? pendencia.tipo_problema
      : `${pendencia.tipo_problema} em ${pendencia.campo}`;
  return `${alvo} · documento ${referenciaDoDocumento(pendencia.documento_id)}`;
}

function Contador({
  rotulo,
  valor,
  alerta = false,
}: {
  rotulo: string;
  valor: number;
  alerta?: boolean;
}) {
  return (
    <div
      className={`rounded-xl border p-3 ${
        alerta ? "border-red-200 bg-red-50" : "border-line bg-canvas"
      }`}
    >
      <p
        className={`m-0 text-2xl font-semibold tracking-[-0.02em] ${
          alerta ? "text-danger" : "text-ink"
        }`}
      >
        {valor}
      </p>
      <p className={`m-0 text-xs ${alerta ? "font-semibold text-red-700" : "text-muted"}`}>
        {rotulo}
      </p>
    </div>
  );
}

/**
 * A fila de conferência: o que está pendente, para quem, e até quando.
 *
 * Server Component, e os filtros vivem na URL (`searchParams`) em vez de estado
 * de cliente — ver a docstring de `components/pendencias/filtros.ts`.
 *
 * Quem pode agir aqui não são os três papéis: `PATCH /api/pendencias/{id}` exige
 * conferente ou coordenador (ADR 0001 — o gestor lê a operação e não faz
 * conferência). A ação some para o gestor, o que é ergonomia; a autoridade
 * continua sendo o 403 da API, tratado em `AcaoPendencia`.
 */
export default async function PendenciasPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const filtros = lerFiltros(await searchParams);
  // Memoizado por `cache` dentro desta renderização: o layout do grupo já
  // perguntou quem está logado e esta chamada reaproveita a resposta.
  const usuario = await usuarioDaSessao();
  if (usuario === null) {
    redirect("/login");
  }

  const base = apiUrl();
  const opcoes = await opcoesAutenticadas();

  let carga: [ResumoPendencias, RespostaPaginada<PendenciaItem>, Operadora[]];
  try {
    // Em paralelo: nenhuma das três depende do resultado da outra, e em série
    // a tela esperaria três vezes o mesmo tempo de rede.
    carga = await Promise.all([
      resumoPendencias(base, opcoes),
      listarPendencias(
        base,
        {
          status: filtros.status,
          deadline: filtros.deadline,
          operadora_id: filtros.operadora_id,
          limite: LIMITE_POR_PAGINA,
          offset: filtros.offset,
        },
        opcoes,
      ),
      listarOperadoras(base, opcoes),
    ]);
  } catch (erro) {
    if (erro instanceof ApiError && erro.status === 401) {
      // A sessão morreu depois de o layout tê-la validado — expiração, logout
      // em outra aba, login novo no mesmo navegador. Por Partial Rendering o
      // layout não roda de novo a cada navegação dentro do grupo, então é esta
      // chamada que descobre e manda a pessoa de volta ao login.
      redirect("/login?motivo=sessao-encerrada");
    }
    // API fora do ar ou com defeito não vira tela vazia fingindo que não há
    // pendência: o erro sobe.
    throw erro;
  }

  const [resumo, pagina, operadoras] = carga;
  const { total } = pagina.paginacao;
  const venceu = marcadorDeVencimento();
  const podeTransicionar = usuario.papel !== "gestor";
  const filtrando = temFiltro(filtros);
  const primeiroDaPagina = filtros.offset + 1;
  const ultimoDaPagina = filtros.offset + pagina.data.length;

  return (
    <div className="grid gap-6">
      <div className="page-head">
        <p className="eyebrow">Operação</p>
        <h1>Pendências</h1>
        <p>
          Fila de conferência das evoluções: o que precisa de correção antes do envio à operadora,
          quem está responsável e até quando.
        </p>
      </div>

      <section className="panel">
        <div className="panel-heading">
          <h2>Resumo</h2>
          {/* O resumo é da operação inteira. Sem dizer isto, ele é lido como o
              total do que a lista abaixo está mostrando. */}
          <span className="text-xs text-muted">Toda a operação, sem os filtros abaixo</span>
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          {STATUS_DE_PENDENCIA.map((status) => (
            <Contador key={status} rotulo={ROTULO_DE_STATUS[status]} valor={resumo.por_status[status]} />
          ))}
        </div>

        <p className="mt-5 mb-3 text-[11px] font-bold uppercase tracking-[0.18em] text-muted">
          Prazo · apenas pendências não resolvidas
        </p>
        <div className="grid gap-3 sm:grid-cols-3">
          {/* `vencidas` em destaque: é o único número desta tela que diz que
              alguém precisa agir hoje. */}
          <Contador rotulo="Vencidas" valor={resumo.por_faixa_deadline.vencidas} alerta />
          <Contador rotulo="Vencem em 7 dias" valor={resumo.por_faixa_deadline.proximos_7_dias} />
          <Contador rotulo="Futuras" valor={resumo.por_faixa_deadline.futuras} />
        </div>
      </section>

      <FiltrosPendencias filtros={filtros} operadoras={operadoras} />

      <section className="panel">
        <div className="panel-heading">
          <h2>{filtrando ? "Pendências filtradas" : "Todas as pendências"}</h2>
          <span className="state state--off">
            {total} {total === 1 ? "pendência" : "pendências"}
          </span>
        </div>

        <AvisosDaFila>
          {!podeTransicionar && (
            <p className="alert--info mb-4">
              Seu papel (gestor) vê a operação inteira, mas não transiciona pendências: a
              conferência é feita por conferente ou coordenador.
            </p>
          )}

          {total === 0 && !filtrando && (
            <p className="empty-state">
              Nenhuma pendência registrada. Elas nascem da classificação automática dos documentos
              enviados — não há como criar uma por aqui.
            </p>
          )}

          {total === 0 && filtrando && (
            <div className="grid justify-items-center gap-3">
              <p className="empty-state w-full">
                Nenhuma pendência com estes filtros. Existem pendências na operação — o resumo
                acima conta todas elas.
              </p>
              <Link href={urlComFiltros({ offset: 0 })} className="btn btn--secondary">
                Limpar filtros
              </Link>
            </div>
          )}

          {total > 0 && pagina.data.length === 0 && (
            <div className="grid justify-items-center gap-3">
              <p className="empty-state w-full">
                Esta página não tem mais itens: a fila encurtou desde que este endereço foi aberto.
              </p>
              <Link href={urlComFiltros({ ...filtros, offset: 0 })} className="btn btn--secondary">
                Voltar à primeira página
              </Link>
            </div>
          )}

          {pagina.data.length > 0 && (
            <ul className="m-0 flex list-none flex-col divide-y divide-line p-0">
              {pagina.data.map((pendencia) => {
                const vencida = venceu(pendencia.deadline, pendencia.status);

                return (
                  <li
                    key={pendencia.id}
                    className="grid gap-3 py-4 first:pt-0 last:pb-0 sm:grid-cols-[1fr_auto] sm:items-start sm:gap-6"
                  >
                    <div className="grid gap-1.5">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className={`state ${VARIANTE_DE_STATUS[pendencia.status]}`}>
                          {ROTULO_DE_STATUS[pendencia.status]}
                        </span>
                        <strong className="text-sm text-ink">{pendencia.tipo_problema}</strong>
                        {pendencia.campo !== null && (
                          <span className="text-xs text-muted">
                            campo <code>{pendencia.campo}</code>
                          </span>
                        )}
                      </div>

                      <p className="m-0 text-sm leading-6 text-muted">{pendencia.descricao}</p>

                      <p className="m-0 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
                        <span>
                          Responsável: <strong className="text-ink">{pendencia.responsavel}</strong>
                        </span>
                        <span>
                          Prazo:{" "}
                          <strong className={vencida ? "text-danger" : "text-ink"}>
                            {formatarPrazo(pendencia.deadline)}
                          </strong>
                        </span>
                        {vencida && <span className="state state--3">Vencida</span>}
                        <span title={`Documento ${pendencia.documento_id}`}>
                          Documento {referenciaDoDocumento(pendencia.documento_id)}
                        </span>
                      </p>
                    </div>

                    {podeTransicionar && (
                      <AcaoPendencia
                        pendenciaId={pendencia.id}
                        status={pendencia.status}
                        nome={nomeDaPendencia(pendencia)}
                        responsavelAtual={pendencia.responsavel}
                      />
                    )}
                  </li>
                );
              })}
            </ul>
          )}

          {total > LIMITE_POR_PAGINA && pagina.data.length > 0 && (
            <nav
              aria-label="Paginação"
              className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4"
            >
              <p className="m-0 text-xs text-muted">
                Mostrando {primeiroDaPagina}–{ultimoDaPagina} de {total}
              </p>
              <div className="flex gap-2">
                {filtros.offset > 0 ? (
                  <Link
                    href={urlComFiltros({
                      ...filtros,
                      offset: Math.max(0, filtros.offset - LIMITE_POR_PAGINA),
                    })}
                    className="btn btn--secondary h-9 min-h-9 px-3 text-xs"
                  >
                    Anterior
                  </Link>
                ) : (
                  <span
                    aria-disabled="true"
                    className="btn btn--secondary h-9 min-h-9 cursor-not-allowed px-3 text-xs opacity-60"
                  >
                    Anterior
                  </span>
                )}

                {ultimoDaPagina < total ? (
                  <Link
                    href={urlComFiltros({
                      ...filtros,
                      offset: filtros.offset + LIMITE_POR_PAGINA,
                    })}
                    className="btn btn--secondary h-9 min-h-9 px-3 text-xs"
                  >
                    Próxima
                  </Link>
                ) : (
                  <span
                    aria-disabled="true"
                    className="btn btn--secondary h-9 min-h-9 cursor-not-allowed px-3 text-xs opacity-60"
                  >
                    Próxima
                  </span>
                )}
              </div>
            </nav>
          )}
        </AvisosDaFila>
      </section>
    </div>
  );
}
