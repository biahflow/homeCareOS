import type { DocumentoStatus, FiltrosConferencia, MetricasParams } from "@homecareos/contracts";
import { STATUS_DE_DOCUMENTO } from "../documentos/vocabulario";

/**
 * Os filtros do relatório vivem na **URL**, não em estado de cliente — o mesmo
 * que a fila de pendências faz, e pelas mesmas três razões: a tela fica
 * compartilhável ("olha este relatório aqui"), sobrevive a uma recarga no meio
 * do turno, e o botão "voltar" do navegador desfaz o último filtro em vez de
 * sair da tela.
 *
 * Aqui há uma quarta razão, específica desta fatia: o **CSV** é um link que
 * carrega os mesmos filtros. Com o filtro na URL, o link é montado a partir da
 * mesma estrutura que a listagem usou, e o arquivo baixado não tem como
 * divergir do que estava na tela.
 *
 * Módulo puro de propósito — sem `next/headers`, sem hooks: é a mesma fonte de
 * verdade para o Server Component que lê a URL e para o componente de filtros
 * que a reescreve.
 */

export const CAMINHO_RELATORIOS = "/relatorios";

/**
 * Itens por página.
 *
 * Abaixo do padrão da API (50) porque a linha deste relatório não é uma linha:
 * são identificação, problema encontrado e ação necessária, e o problema é
 * texto livre de tamanho imprevisível.
 */
export const LIMITE_POR_PAGINA = 25;

export interface FiltrosDeRelatorio {
  /** Competência "AAAA-MM". */
  competencia?: string;
  status?: DocumentoStatus;
  operadora_id?: string;
  /** Data "AAAA-MM-DD": documentos recebidos a partir dela, inclusive. */
  data_inicio?: string;
  /** Data "AAAA-MM-DD": recebidos até ela, **dia inteiro incluído**. */
  data_fim?: string;
  apenas_pendentes: boolean;
  offset: number;
}

/**
 * O vocabulário do documento vem de `components/documentos/vocabulario.ts` —
 * reexportado aqui, e não redefinido, para que esta tela e a listagem de
 * documentos não tenham duas listas de status, dois rótulos e duas cores para o
 * mesmo dado. Os consumidores desta tela continuam importando daqui.
 *
 * `VARIANTE_DE_SEVERIDADE` segue sendo o que esta tela usa: cada linha do
 * relatório traz a `severidade` que a API decidiu, e é ela — nunca o `status` —
 * que escolhe a variante do selo.
 */
export {
  ROTULO_DE_STATUS_DOCUMENTO,
  ROTULO_DE_TIPO_DOCUMENTO,
  STATUS_DE_DOCUMENTO,
  VARIANTE_DE_SEVERIDADE,
} from "../documentos/vocabulario";

type ParametrosDaUrl = Record<string, string | string[] | undefined>;

const PADRAO_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PADRAO_DATA = /^\d{4}-\d{2}-\d{2}$/;

/**
 * Competência `AAAA-MM` com mês entre 01 e 12 — a mesma expressão que a API
 * aplica (`reports/schema.PADRAO_COMPETENCIA`).
 *
 * Ela precisa estar aqui porque a API **recusa** competência malformada com 422
 * em vez de tratá-la como filtro vazio (`_validar_competencia`, e com razão: um
 * `2026-13` silencioso devolveria "nenhum documento", indistinguível de uma
 * competência real e vazia). Sem esta validação, um `?competencia=2026-13`
 * digitado à mão derrubaria a tela inteira num erro de servidor.
 */
const PADRAO_COMPETENCIA = /^\d{4}-(0[1-9]|1[0-2])$/;

/** `?a=1&a=2` chega como array; o primeiro valor é o que vale. */
function primeiro(valor: string | string[] | undefined): string | undefined {
  return Array.isArray(valor) ? valor[0] : valor;
}

/**
 * Traduz a query string em filtros, **descartando o que a API recusaria**.
 *
 * A query string é entrada de fora: qualquer link colado pode conter qualquer
 * coisa, e valor que não reconhecemos vira filtro ausente. A tela continua de pé
 * e os controles mostram "todas" — que é o estado que ela de fato está
 * exibindo.
 */
export function lerFiltros(params: ParametrosDaUrl): FiltrosDeRelatorio {
  const competencia = primeiro(params.competencia);
  const status = primeiro(params.status);
  const operadoraId = primeiro(params.operadora_id);
  const dataInicio = primeiro(params.data_inicio);
  const dataFim = primeiro(params.data_fim);
  const offset = Number(primeiro(params.offset));

  return {
    competencia:
      competencia !== undefined && PADRAO_COMPETENCIA.test(competencia) ? competencia : undefined,
    status: STATUS_DE_DOCUMENTO.find((valido) => valido === status),
    operadora_id:
      operadoraId !== undefined && PADRAO_UUID.test(operadoraId) ? operadoraId : undefined,
    data_inicio: dataInicio !== undefined && PADRAO_DATA.test(dataInicio) ? dataInicio : undefined,
    data_fim: dataFim !== undefined && PADRAO_DATA.test(dataFim) ? dataFim : undefined,
    apenas_pendentes: primeiro(params.apenas_pendentes) === "true",
    offset: Number.isSafeInteger(offset) && offset > 0 ? offset : 0,
  };
}

/** Há algum filtro em vigor? O `offset` não conta: paginar não é filtrar. */
export function temFiltro(filtros: FiltrosDeRelatorio): boolean {
  return (
    filtros.competencia !== undefined ||
    filtros.status !== undefined ||
    filtros.operadora_id !== undefined ||
    filtros.data_inicio !== undefined ||
    filtros.data_fim !== undefined ||
    filtros.apenas_pendentes
  );
}

/**
 * A competência é o **único** filtro em vigor?
 *
 * Serve a uma distinção que a tela precisa fazer e a API não faz por ela: uma
 * competência sem nenhum documento não é a mesma coisa que um recorte que não
 * casou com nada. No primeiro caso o mês está vazio — mudar de mês é a saída; no
 * segundo, foi a combinação de filtros que excluiu tudo, e limpá-los é a saída.
 * Apresentar os dois com a mesma frase manda a pessoa procurar defeito no filtro
 * quando o que existe é um mês sem movimento.
 */
export function apenasCompetenciaFiltrada(filtros: FiltrosDeRelatorio): boolean {
  return (
    filtros.competencia !== undefined &&
    filtros.status === undefined &&
    filtros.operadora_id === undefined &&
    filtros.data_inicio === undefined &&
    filtros.data_fim === undefined &&
    !filtros.apenas_pendentes
  );
}

/**
 * Os filtros no formato que a API do relatório espera — e a **única** conversão
 * do caminho.
 *
 * A listagem JSON e a URL do CSV partem daqui, então o arquivo baixado carrega
 * exatamente os filtros da tela. `paciente_id` existe no contrato e não é
 * preenchido: esta tela não tem de onde escolher um paciente (não há endpoint de
 * listagem), e nome de paciente nunca vira filtro de URL.
 */
export function filtrosDaApi(filtros: FiltrosDeRelatorio): FiltrosConferencia {
  return {
    competencia: filtros.competencia,
    status: filtros.status,
    operadora_id: filtros.operadora_id,
    data_inicio: filtros.data_inicio,
    data_fim: filtros.data_fim,
    apenas_pendentes: filtros.apenas_pendentes,
  };
}

/**
 * A janela de métricas correspondente a estes filtros.
 *
 * `/metricas` aceita **competência e operadora**, e não status nem período: são
 * endpoints diferentes, com filtros diferentes. Com uma competência escolhida a
 * janela é aquele mês (início e fim iguais); sem ela, a API decide — as 12
 * competências mais recentes. A tela precisa dizer isso em voz alta, senão os
 * dois painéis parecem falar do mesmo recorte quando não falam.
 */
export function metricasDaApi(filtros: FiltrosDeRelatorio): MetricasParams {
  return {
    competencia_inicio: filtros.competencia,
    competencia_fim: filtros.competencia,
    operadora_id: filtros.operadora_id,
  };
}

/**
 * O endereço do relatório com estes filtros — o único lugar que monta esta URL.
 *
 * Omite o que está vazio para a barra de endereços continuar legível, e omite
 * `offset=0` e `apenas_pendentes=false` porque são os defaults: sem isso, dois
 * endereços diferentes mostrariam a mesma tela.
 */
export function urlComFiltros(filtros: FiltrosDeRelatorio): string {
  const busca = new URLSearchParams();
  if (filtros.competencia !== undefined) busca.set("competencia", filtros.competencia);
  if (filtros.status !== undefined) busca.set("status", filtros.status);
  if (filtros.operadora_id !== undefined) busca.set("operadora_id", filtros.operadora_id);
  if (filtros.data_inicio !== undefined) busca.set("data_inicio", filtros.data_inicio);
  if (filtros.data_fim !== undefined) busca.set("data_fim", filtros.data_fim);
  if (filtros.apenas_pendentes) busca.set("apenas_pendentes", "true");
  if (filtros.offset > 0) busca.set("offset", String(filtros.offset));

  const texto = busca.toString();
  return texto === "" ? CAMINHO_RELATORIOS : `${CAMINHO_RELATORIOS}?${texto}`;
}
