import Link from "next/link";
import { redirect } from "next/navigation";
import { ApiError, listarDocumentos, listarOperadoras } from "@homecareos/contracts";
import type { DocumentoListItem, Operadora, RespostaPaginada } from "@homecareos/contracts";
import {
  LIMITE_POR_PAGINA,
  apenasCompetenciaFiltrada,
  filtrosDaApi,
  lerFiltros,
  temFiltro,
  urlComFiltros,
  urlDoDocumento,
} from "@/components/documentos/filtros";
import { FiltrosDocumentos } from "@/components/documentos/FiltrosDocumentos";
import { FormularioUpload } from "@/components/documentos/FormularioUpload";
import {
  ROTULO_DE_STATUS_DOCUMENTO,
  ROTULO_DE_TIPO_DOCUMENTO,
  varianteDeStatus,
} from "@/components/documentos/vocabulario";
// Formatadores genéricos (`Intl` configurado com o fuso da operação) que
// nasceram na tela de relatórios. Importados, e não copiados: duas
// configurações de `Intl` no mesmo app divergem na primeira mudança, e uma data
// que muda de dia entre duas telas do mesmo sistema é indefensável.
import {
  formatarCompetencia,
  formatarDataHora,
  formatarInteiro,
  referenciaDoDocumento,
} from "@/components/relatorios/formatos";
import { apiUrl, opcoesAutenticadas } from "@/lib/api-servidor";
import { usuarioDaSessao } from "@/lib/sessao";

/**
 * Avisos que a própria aplicação pede pela URL ao mandar alguém para cá — quem
 * tentou abrir `/usuarios` sem ser coordenador, ou `/alertas` sem ser
 * coordenador nem gestor. Esta é a tela para onde o login leva, e por isso a
 * que recebe quem foi recusado em outra.
 *
 * Mesma regra da tela de login: o valor que vem na query **nunca** é
 * renderizado, só serve de chave neste mapa. Ecoar o parâmetro cru deixaria
 * qualquer link montado por terceiro escrever o que quisesse na tela de quem
 * está logado.
 */
const AVISOS: Record<string, string> = {
  "usuarios-restrito":
    "A administração de usuários é do papel de coordenador. Se você precisa cadastrar ou desativar alguém, peça a um coordenador.",
  "alertas-restrito":
    "O log de alertas de WhatsApp é de coordenador e gestor. Se você precisa saber por que um aviso não chegou, peça a um deles.",
};

/**
 * Documentos em conferência: o envio e a fila do que já foi enviado.
 *
 * Server Component, com os filtros na URL (`searchParams`) — ver a docstring de
 * `components/documentos/filtros.ts`. O upload continua sendo Client Component
 * (`FormularioUpload`), porque é o navegador que tem o arquivo.
 *
 * Duas coisas desta tela são contrato, e não escolha de layout:
 *
 * 1. **O documento escaneado não aparece nesta lista, por layout, não por
 *    limitação.** `GET /api/documentos/{id}/arquivo` (PR #54) serve a página
 *    escaneada, mas esta tela é uma lista de triagem — sem espaço para exibir
 *    uma imagem de verdade por linha. Ela aparece no detalhe de cada
 *    documento (`ImagemDocumento`), onde há espaço para isso.
 * 2. **A cor do status é a mesma do relatório.** Ela sai de
 *    `components/documentos/vocabulario.ts`, que espelha
 *    `reports/conferencia._SEVERIDADE_POR_STATUS` porque esta rota não devolve
 *    severidade. É o único mapa de status para cor de `apps/web`.
 */
export default async function DocumentosPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const parametros = await searchParams;
  const filtros = lerFiltros(parametros);
  const aviso = typeof parametros.motivo === "string" ? AVISOS[parametros.motivo] : undefined;
  // Memoizado por `cache` dentro desta renderização: o layout do grupo já
  // perguntou quem está logado e esta chamada reaproveita a resposta.
  const usuario = await usuarioDaSessao();
  if (usuario === null) {
    redirect("/login");
  }

  const base = apiUrl();
  const opcoes = await opcoesAutenticadas();

  let carga: [RespostaPaginada<DocumentoListItem>, Operadora[]];
  try {
    // Em paralelo: a lista de operadoras não depende da listagem, e em série a
    // tela esperaria duas vezes o mesmo tempo de rede.
    carga = await Promise.all([
      listarDocumentos(
        base,
        { ...filtrosDaApi(filtros), limite: LIMITE_POR_PAGINA, offset: filtros.offset },
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
    // documento: o erro sobe.
    throw erro;
  }

  const [pagina, operadoras] = carga;
  const { total } = pagina.paginacao;
  const filtrando = temFiltro(filtros);
  const competenciaVazia = apenasCompetenciaFiltrada(filtros);
  const primeiroDaPagina = filtros.offset + 1;
  const ultimoDaPagina = filtros.offset + pagina.data.length;
  const nomeDaOperadora = (operadoraId: string | null): string =>
    operadoraId === null
      ? "sem operadora"
      : (operadoras.find((operadora) => operadora.id === operadoraId)?.nome ??
        "operadora não encontrada na lista");

  return (
    <div className="grid gap-6">
      <div className="page-head">
        <p className="eyebrow">Operação</p>
        <h1>Documentos</h1>
        <p>
          Envie a evolução de prontuário escaneada (PDF ou imagem) para conferência antes do envio
          à operadora, e acompanhe aqui o que já foi enviado.
        </p>
      </div>

      {aviso && (
        <p role="status" className="alert--info">
          {aviso}
        </p>
      )}

      <FormularioUpload />

      <FiltrosDocumentos filtros={filtros} operadoras={operadoras} />

      <section className="panel">
        <div className="panel-heading">
          <h2>{filtrando ? "Documentos filtrados" : "Todos os documentos"}</h2>
          <span className="state state--off">
            {formatarInteiro(total)} {total === 1 ? "documento" : "documentos"}
          </span>
        </div>

        {total === 0 && !filtrando && (
          <p className="empty-state">
            Nenhum documento enviado ainda. Use o formulário acima — a fila desta tela nasce dos
            envios.
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
            <Link href={urlComFiltros({ offset: 0 })} className="btn btn--secondary">
              Ver todas as competências
            </Link>
          </div>
        )}

        {total === 0 && filtrando && !competenciaVazia && (
          <div className="grid justify-items-center gap-3">
            <p className="empty-state w-full">
              Nenhum documento com estes filtros. Existem documentos na operação — o recorte é que
              não alcançou nenhum.
            </p>
            <Link href={urlComFiltros({ offset: 0 })} className="btn btn--secondary">
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
            {pagina.data.map((documento) => (
              <li
                key={documento.id}
                className="grid gap-3 py-4 first:pt-0 last:pb-0 sm:grid-cols-[1fr_auto] sm:items-center sm:gap-6"
              >
                <div className="grid gap-1.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={`state ${varianteDeStatus(documento.status)}`}>
                      {ROTULO_DE_STATUS_DOCUMENTO[documento.status]}
                    </span>
                    <strong className="text-sm text-ink">
                      {ROTULO_DE_TIPO_DOCUMENTO[documento.tipo]}
                    </strong>
                    <span className="text-xs text-muted" title={`Documento ${documento.id}`}>
                      Documento {referenciaDoDocumento(documento.id)}
                    </span>
                  </div>

                  <p className="m-0 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
                    <span className="first-letter:uppercase">
                      {formatarCompetencia(documento.competencia)}
                    </span>
                    <span>
                      Operadora:{" "}
                      <strong className="text-ink">
                        {nomeDaOperadora(documento.operadora_id)}
                      </strong>
                    </span>
                    {/* Página nula não é página zero: o documento simplesmente
                        não veio de um PDF multi-página. */}
                    <span>
                      {documento.pagina === null
                        ? "Página única"
                        : `Página ${formatarInteiro(documento.pagina)}`}
                    </span>
                    <span>
                      Paciente:{" "}
                      <strong className="text-ink">
                        {documento.paciente_id === null ? "não vinculado" : "vinculado"}
                      </strong>
                    </span>
                  </p>

                  <p className="m-0 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
                    <span>Recebido em {formatarDataHora(documento.created_at)}</span>
                    <span>Atualizado em {formatarDataHora(documento.updated_at)}</span>
                  </p>
                </div>

                <Link
                  href={urlDoDocumento(documento.id)}
                  className="btn btn--secondary h-9 min-h-9 w-fit px-3 text-xs"
                >
                  Abrir conferência
                </Link>
              </li>
            ))}
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
