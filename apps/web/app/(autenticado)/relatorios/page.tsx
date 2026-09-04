import Link from "next/link";
import { redirect } from "next/navigation";
import {
  ApiError,
  listarBaselines,
  listarOperadoras,
  metricas,
  relatorioConferencia,
  urlRelatorioConferenciaCsv,
} from "@homecareos/contracts";
import type {
  BaselineOut,
  LinhaConferencia,
  MetricasResponse,
  Operadora,
  OpcoesRequisicao,
  RespostaPaginada,
} from "@homecareos/contracts";
import { CartaoCompetencia } from "@/components/relatorios/CartaoCompetencia";
import {
  LIMITE_POR_PAGINA,
  ROTULO_DE_STATUS_DOCUMENTO,
  ROTULO_DE_TIPO_DOCUMENTO,
  VARIANTE_DE_SEVERIDADE,
  apenasCompetenciaFiltrada,
  filtrosDaApi,
  lerFiltros,
  metricasDaApi,
  temFiltro,
  urlComFiltros,
} from "@/components/relatorios/filtros";
import type { FiltrosDeRelatorio } from "@/components/relatorios/filtros";
import { FiltrosRelatorio } from "@/components/relatorios/FiltrosRelatorio";
import { FormularioBaseline } from "@/components/relatorios/FormularioBaseline";
import { MetricasPorOperadora } from "@/components/relatorios/MetricasPorOperadora";
import { PainelComparacaoGlosa } from "@/components/relatorios/PainelComparacaoGlosa";
import { VolumePorDia } from "@/components/relatorios/VolumePorDia";
import {
  formatarCompetencia,
  formatarDataHora,
  formatarDataIso,
  formatarInteiro,
  formatarReais,
  problemasDaLinha,
  referenciaDoDocumento,
} from "@/components/relatorios/formatos";
import { apiUrl, opcoesAutenticadas } from "@/lib/api-servidor";
import { API_BASE_URL } from "@/lib/env";
import { usuarioDaSessao } from "@/lib/sessao";

/**
 * O que a carga de gestão (`/metricas` e `/baseline`) devolveu.
 *
 * Ela é separada da listagem porque os dois grupos têm **públicos diferentes**,
 * e é isto que este router faz de exceção consciente à regra "auth por router":
 * o relatório de conferência é dos três papéis, as métricas e os baselines são
 * de coordenador e gestor, e escrever baseline é só do gestor. Uma conferente
 * abrindo esta tela recebe 403 nas duas chamadas de gestão — e isso não pode
 * derrubar a listagem que é justamente a dela.
 */
type CargaDeGestao =
  | { tipo: "ok"; metricas: MetricasResponse; baselines: BaselineOut[] }
  | { tipo: "sem-permissao" }
  | { tipo: "papel-sem-gestao" };

/**
 * Busca métricas e baselines convertendo o **403 em resultado**, não em exceção.
 *
 * O 401 continua subindo: sessão encerrada é tratada no mesmo lugar que a da
 * listagem, e mandar a pessoa para o login é a única reação correta.
 */
async function carregarGestao(
  base: string,
  filtros: FiltrosDeRelatorio,
  opcoes: OpcoesRequisicao,
): Promise<CargaDeGestao> {
  try {
    const [resposta, baselines] = await Promise.all([
      metricas(base, metricasDaApi(filtros), opcoes),
      listarBaselines(base, opcoes),
    ]);
    return { tipo: "ok", metricas: resposta, baselines };
  } catch (erro) {
    if (erro instanceof ApiError && erro.status === 403) {
      // O papel mudou no servidor depois de esta requisição ler quem é a
      // pessoa. Raro, mas a tela continua de pé sem o painel de gestão em vez
      // de virar um erro de servidor.
      return { tipo: "sem-permissao" };
    }
    throw erro;
  }
}

function AvisoDeGestao({ mensagem }: { mensagem: string }) {
  return <p className="alert--info">{mensagem}</p>;
}

/**
 * Relatórios e métricas da conferência.
 *
 * Server Component, com os filtros na URL (`searchParams`) — ver a docstring de
 * `components/relatorios/filtros.ts`. Cinco coisas desta tela são contrato, e não
 * escolha de layout:
 *
 * 1. **Os dois blocos de métrica nunca se fundem.** Ver `CartaoCompetencia`.
 * 2. **`glosa_informada: null` é "ninguém informou", nunca zero.** Em nenhum
 *    lugar desta tela aparece 0% de glosa por falta de baseline.
 * 3. **A cor da linha vem de `severidade`, decidida pela API.** Não há
 *    mapeamento de status para cor aqui — `severidade_de` é a autoridade, e
 *    duplicá-la criaria uma segunda regra para divergir da primeira.
 * 4. **`comparacao_glosa: null` também é ausência, não zero.** Ver
 *    `PainelComparacaoGlosa` — a mesma regra do item 2, aplicada ao
 *    antes/depois entre competências.
 * 5. **A linha de operadora `null`, em `por_operadora`, nunca é escondida.**
 *    Ver `MetricasPorOperadora` — é ela que agrupa os documentos que ninguém
 *    conseguiu vincular.
 *
 * A autorização é o **inverso** da fila de pendências: lá o gestor não age;
 * aqui ele é o único que escreve (o baseline). Esconder o formulário para os
 * outros papéis é ergonomia; a autoridade é o 403 da API.
 */
export default async function RelatoriosPage({
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

  const podeVerGestao = usuario.papel === "coordenador" || usuario.papel === "gestor";
  const podeRegistrarBaseline = usuario.papel === "gestor";

  let carga: [RespostaPaginada<LinhaConferencia>, Operadora[], CargaDeGestao];
  try {
    // Tudo num `Promise.all` só: nenhuma das cargas depende do resultado da
    // outra, e em série a tela esperaria três vezes o mesmo tempo de rede. A de
    // gestão entra já embrulhada (`carregarGestao`) para que um 403 dela não
    // leve junto a listagem — e para que nenhuma promessa fique sem tratamento
    // enquanto as outras são aguardadas.
    carga = await Promise.all([
      relatorioConferencia(
        base,
        { ...filtrosDaApi(filtros), limite: LIMITE_POR_PAGINA, offset: filtros.offset },
        opcoes,
      ),
      listarOperadoras(base, opcoes),
      podeVerGestao
        ? carregarGestao(base, filtros, opcoes)
        : Promise.resolve<CargaDeGestao>({ tipo: "papel-sem-gestao" }),
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
    // documento: o erro sobe.
    throw erro;
  }

  const [pagina, operadoras, gestao] = carga;
  const { total } = pagina.paginacao;
  const filtrando = temFiltro(filtros);
  const competenciaVazia = apenasCompetenciaFiltrada(filtros);
  const primeiroDaPagina = filtros.offset + 1;
  const ultimoDaPagina = filtros.offset + pagina.data.length;
  // A listagem e o CSV partem do mesmo objeto de filtros: o arquivo baixado não
  // tem como divergir do que está na tela.
  const filtrosDaListagem = filtrosDaApi(filtros);
  const urlDoCsv = urlRelatorioConferenciaCsv(API_BASE_URL, filtrosDaListagem);
  // Mais recente primeiro: a API devolve em ordem crescente, e quem abre o
  // painel quer o mês corrente no topo.
  const competencias =
    gestao.tipo === "ok" ? [...gestao.metricas.competencias].reverse() : [];

  return (
    <div className="grid gap-6">
      <div className="page-head">
        <p className="eyebrow">Operação</p>
        <h1>Relatórios</h1>
        <p>
          O relatório de conferência da competência, linha a linha, e as métricas agregadas do que
          a conferência mediu e do que a operadora glosou.
        </p>
      </div>

      <FiltrosRelatorio filtros={filtros} operadoras={operadoras} />

      <section className="panel">
        <div className="panel-heading">
          <h2>Métricas por competência</h2>
          {/* A janela só é anunciada quando há painel para ela descrever:
              anunciá-la ao lado de "você não vê isto" descreveria um recorte
              que a pessoa não está vendo. */}
          {gestao.tipo === "ok" && (
            <span className="text-xs text-muted">
              {filtros.competencia === undefined
                ? "12 competências mais recentes"
                : formatarCompetencia(filtros.competencia)}
            </span>
          )}
        </div>

        {gestao.tipo === "papel-sem-gestao" && (
          <AvisoDeGestao mensagem="Métricas agregadas e baselines são leitura de gestão (coordenador ou gestor). O relatório de conferência abaixo, que é o do dia a dia, continua disponível para você." />
        )}

        {gestao.tipo === "sem-permissao" && (
          <AvisoDeGestao mensagem="A API recusou o acesso às métricas agregadas para o seu papel. Se ele mudou agora há pouco, entre de novo; o relatório de conferência abaixo continua disponível." />
        )}

        {gestao.tipo === "ok" && (
          <>
            <p className="alert--info mb-4">
              Só <strong>competência</strong> e <strong>operadora</strong> valem para este painel —
              status, período e “só com pendência” filtram apenas a listagem abaixo, porque{" "}
              <code>/metricas</code> não os aceita.
            </p>

            {competencias.length === 0 ? (
              <p className="empty-state">
                Nenhum documento nesta janela de competências. As métricas nascem dos documentos
                recebidos — não há como registrá-las por aqui.
              </p>
            ) : (
              <div className="grid gap-4">
                {competencias.map((competencia) => (
                  <CartaoCompetencia
                    key={competencia.competencia}
                    metricas={competencia}
                    podeRegistrarBaseline={podeRegistrarBaseline}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </section>

      {gestao.tipo === "ok" && (
        <section className="panel">
          <div className="panel-heading">
            <h2>Documentos por operadora</h2>
          </div>
          <p className="alert--info mb-4">
            Mesma janela de competências dos cartões acima. A linha sem operadora agrupa os
            documentos que ninguém conseguiu vincular — é a que mais interessa olhar.
          </p>
          <MetricasPorOperadora porOperadora={gestao.metricas.por_operadora} />
        </section>
      )}

      {gestao.tipo === "ok" && (
        <section className="panel">
          <div className="panel-heading">
            <h2>Documentos por dia</h2>
          </div>
          <p className="alert--info mb-4">
            Para enxergar o pico do fechamento, na mesma janela de competências dos cartões acima.
          </p>
          <VolumePorDia dias={gestao.metricas.por_dia} />
        </section>
      )}

      {gestao.tipo === "ok" && (
        <section className="panel">
          <div className="panel-heading">
            <h2>Comparação de glosa</h2>
          </div>
          <PainelComparacaoGlosa
            comparacao={gestao.metricas.comparacao_glosa}
            competencias={gestao.metricas.competencias}
          />
        </section>
      )}

      {gestao.tipo === "ok" && (
        <section className="panel">
          <div className="panel-heading">
            <h2>Baseline de glosa</h2>
            <span className="state state--off">
              {gestao.baselines.length}{" "}
              {gestao.baselines.length === 1 ? "registrado" : "registrados"}
            </span>
          </div>

          <p className="mb-4 text-sm leading-6 text-muted">
            O baseline é o número de glosa <strong>informado pela operadora</strong> — a régua
            contra a qual a conferência é medida. Ele é digitado de um demonstrativo e nunca é
            calculado pelo sistema.
          </p>

          {gestao.baselines.length === 0 ? (
            <p className="empty-state">
              Nenhum baseline registrado. Enquanto não houver, o bloco de glosa das competências
              acima aparece como <strong>não informado</strong> — que é diferente de glosa zero.
            </p>
          ) : (
            <ul className="m-0 flex list-none flex-col divide-y divide-line p-0">
              {gestao.baselines.map((baseline) => (
                <li key={baseline.id} className="grid gap-1 py-3 first:pt-0 last:pb-0">
                  <p className="m-0 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
                    <strong className="text-ink first-letter:uppercase">
                      {formatarCompetencia(baseline.competencia)}
                    </strong>
                    <span className="text-xs text-muted">
                      {baseline.operadora_id === null
                        ? "Consolidado (todas as operadoras)"
                        : (operadoras.find((operadora) => operadora.id === baseline.operadora_id)
                            ?.nome ?? "Operadora não encontrada na lista")}
                    </span>
                  </p>
                  <p className="m-0 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
                    <span>
                      {formatarInteiro(baseline.documentos_glosados)} glosados de{" "}
                      {formatarInteiro(baseline.documentos_enviados)} enviados
                    </span>
                    {baseline.valor_glosado_centavos !== null && (
                      <span>{formatarReais(baseline.valor_glosado_centavos)}</span>
                    )}
                    <span>
                      Fonte: <strong className="text-ink">{baseline.fonte}</strong>
                    </span>
                    <span>Atualizado em {formatarDataHora(baseline.updated_at)}</span>
                  </p>
                  {baseline.observacao !== null && (
                    <p className="m-0 text-xs text-muted">{baseline.observacao}</p>
                  )}
                </li>
              ))}
            </ul>
          )}

          {podeRegistrarBaseline ? (
            <div className="mt-5 border-t border-line pt-5">
              <h3 className="m-0 mb-3 text-sm font-semibold text-ink">Registrar ou corrigir</h3>
              <FormularioBaseline operadoras={operadoras} baselines={gestao.baselines} />
            </div>
          ) : (
            <p className="alert--info mt-4">
              Seu papel lê os baselines, mas não os registra: o baseline é a régua contra a qual o
              próprio sistema é medido, e escrevê-la cabe ao gestor (ADR 0001).
            </p>
          )}
        </section>
      )}

      <section className="panel">
        <div className="panel-heading">
          <h2>{filtrando ? "Conferência filtrada" : "Conferência da operação"}</h2>
          <span className="state state--off">
            {formatarInteiro(total)} {total === 1 ? "documento" : "documentos"}
          </span>
        </div>

        {total > 0 && (
          <div className="mb-4 grid gap-2 rounded-xl border border-line bg-canvas p-4 sm:flex sm:items-center sm:justify-between sm:gap-4">
            <p className="m-0 text-xs leading-5 text-muted">
              O CSV sai com <strong>os mesmos filtros</strong> desta tela e sem paginação — o
              extrato inteiro. Ele contém <strong>nome de paciente</strong> e fica salvo na pasta de
              downloads desta máquina como qualquer outro arquivo.
            </p>
            {/* Link comum, e não `fetch` + blob: a URL é same-origin (proxy do
                ADR 0002), o cookie viaja sozinho, o navegador cuida do nome do
                arquivo e a API transmite o CSV em blocos — o streaming que
                `_stream_csv` faz questão de manter. */}
            <a href={urlDoCsv} className="btn btn--secondary shrink-0">
              Baixar CSV
            </a>
          </div>
        )}

        {total === 0 && !filtrando && (
          <p className="empty-state">
            Nenhum documento recebido ainda. As linhas deste relatório nascem dos documentos
            enviados para conferência — não há como criar uma por aqui.
          </p>
        )}

        {/* Competência vazia e recorte que não casou são coisas diferentes, e a
            saída de cada uma é diferente: no primeiro caso o mês não teve
            movimento, e o que resolve é trocar de mês; no segundo foi a
            combinação de filtros que excluiu tudo. */}
        {total === 0 && competenciaVazia && (
          <div className="grid justify-items-center gap-3">
            <p className="empty-state w-full">
              Nenhum documento em{" "}
              <strong className="text-ink">{formatarCompetencia(filtros.competencia ?? "")}</strong>
              . Não é o recorte: esta competência não recebeu documento nenhum.
            </p>
            <Link href={urlComFiltros({ apenas_pendentes: false, offset: 0 })} className="btn btn--secondary">
              Ver todas as competências
            </Link>
          </div>
        )}

        {total === 0 && filtrando && !competenciaVazia && (
          <div className="grid justify-items-center gap-3">
            <p className="empty-state w-full">
              Nenhum documento casou com estes filtros. Existem documentos na operação — o recorte é
              que não alcançou nenhum.
            </p>
            <Link href={urlComFiltros({ apenas_pendentes: false, offset: 0 })} className="btn btn--secondary">
              Limpar filtros
            </Link>
          </div>
        )}

        {total > 0 && pagina.data.length === 0 && (
          <div className="grid justify-items-center gap-3">
            <p className="empty-state w-full">
              Esta página não tem mais itens: o conjunto encurtou desde que este endereço foi
              aberto.
            </p>
            <Link href={urlComFiltros({ ...filtros, offset: 0 })} className="btn btn--secondary">
              Voltar à primeira página
            </Link>
          </div>
        )}

        {pagina.data.length > 0 && (
          <ul className="m-0 flex list-none flex-col divide-y divide-line p-0">
            {pagina.data.map((linha) => {
              const problemas = problemasDaLinha(linha.problema_encontrado);

              return (
                <li
                  key={linha.documento_id}
                  className="grid gap-3 py-4 first:pt-0 last:pb-0 sm:grid-cols-[1fr_18rem] sm:items-start sm:gap-6"
                >
                  <div className="grid gap-1.5">
                    <div className="flex flex-wrap items-center gap-2">
                      {/* A variante vem da `severidade` que a API decidiu, nunca
                          do status: o texto nomeia o status, a cor obedece à
                          regra de produto do backend. */}
                      <span className={`state ${VARIANTE_DE_SEVERIDADE[linha.severidade]}`}>
                        {ROTULO_DE_STATUS_DOCUMENTO[linha.status]}
                      </span>
                      <strong className="text-sm text-ink">
                        {ROTULO_DE_TIPO_DOCUMENTO[linha.tipo]}
                      </strong>
                      <span className="text-xs text-muted" title={`Documento ${linha.documento_id}`}>
                        Documento {referenciaDoDocumento(linha.documento_id)}
                      </span>
                    </div>

                    <p className="m-0 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
                      <span>
                        Paciente:{" "}
                        <strong className="text-ink">
                          {linha.paciente_nome ?? "não vinculado"}
                        </strong>
                      </span>
                      <span>
                        Operadora:{" "}
                        <strong className="text-ink">{linha.operadora_nome ?? "sem operadora"}</strong>
                      </span>
                      <span>Competência {linha.competencia}</span>
                    </p>

                    <p className="m-0 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
                      <span>Recebido em {formatarDataHora(linha.recebido_em)}</span>
                      <span>
                        Atendimento:{" "}
                        {linha.data_atendimento === null
                          ? "não extraído"
                          : formatarDataIso(linha.data_atendimento)}
                      </span>
                    </p>

                    <div className="mt-1">
                      <p className="m-0 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">
                        Problema encontrado
                      </p>
                      {problemas.length === 0 ? (
                        <p className="m-0 text-sm leading-6 text-muted">Nenhum problema aberto.</p>
                      ) : (
                        <ul className="m-0 list-disc pl-5 text-sm leading-6 text-muted">
                          {problemas.map((problema, indice) => (
                            <li key={`${indice}-${problema}`}>{problema}</li>
                          ))}
                        </ul>
                      )}
                    </div>
                  </div>

                  <div className="grid content-start gap-1.5 rounded-xl border border-line bg-canvas p-3">
                    <p className="m-0 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">
                      Ação necessária
                    </p>
                    <p className="m-0 text-sm leading-6 text-ink">{linha.acao_necessaria}</p>
                    <p className="m-0 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted">
                      <span>
                        {formatarInteiro(linha.pendencias_abertas)}{" "}
                        {linha.pendencias_abertas === 1 ? "pendência aberta" : "pendências abertas"}
                      </span>
                      {linha.deadline !== null && (
                        <span>Prazo {formatarDataHora(linha.deadline)}</span>
                      )}
                    </p>
                  </div>
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
              Mostrando {primeiroDaPagina}–{ultimoDaPagina} de {formatarInteiro(total)}
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
                  href={urlComFiltros({ ...filtros, offset: filtros.offset + LIMITE_POR_PAGINA })}
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
      </section>
    </div>
  );
}
