import type { DocumentoStatus, ListarDocumentosParams } from "@homecareos/contracts";
import { STATUS_DE_DOCUMENTO } from "./vocabulario";

/**
 * Os filtros da listagem de documentos vivem na **URL**, não em estado de
 * cliente — o mesmo que a fila de pendências e o relatório fazem, e pelas
 * mesmas três razões: a listagem fica compartilhável ("olha este documento
 * aqui"), sobrevive a uma recarga no meio do turno, e o botão "voltar" do
 * navegador desfaz o último filtro em vez de sair da tela.
 *
 * Aqui há uma quarta razão, específica desta tela: o **upload** fica na mesma
 * página. Depois de enviar, a listagem é recarregada do servidor
 * (`router.refresh()`) e precisa voltar com o mesmo recorte que estava na tela;
 * com o filtro em estado de cliente, o refresh o perderia ou o duplicaria.
 *
 * Módulo puro de propósito — sem `next/headers`, sem hooks: é a mesma fonte de
 * verdade para o Server Component que lê a URL e para o componente de filtros
 * que a reescreve.
 */

export const CAMINHO_DOCUMENTOS = "/documentos";

/**
 * Itens por página.
 *
 * Abaixo do padrão da API (50) porque a página inteira também carrega o
 * formulário de upload: cinquenta linhas empurram o envio para fora da tela e
 * deixam a listagem sem começo visível.
 */
export const LIMITE_POR_PAGINA = 25;

export interface FiltrosDeDocumentos {
  /** Competência "AAAA-MM". */
  competencia?: string;
  status?: DocumentoStatus;
  operadora_id?: string;
  offset: number;
}

type ParametrosDaUrl = Record<string, string | string[] | undefined>;

const PADRAO_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Competência `AAAA-MM` com mês entre 01 e 12 — o formato que o `<input type="month">` produz. */
const PADRAO_COMPETENCIA = /^\d{4}-(0[1-9]|1[0-2])$/;

/** `?a=1&a=2` chega como array; o primeiro valor é o que vale. */
function primeiro(valor: string | string[] | undefined): string | undefined {
  return Array.isArray(valor) ? valor[0] : valor;
}

/**
 * Traduz a query string em filtros, **descartando o que não é filtro válido**.
 *
 * A query string é entrada de fora: qualquer link colado pode conter qualquer
 * coisa. Um `?status=urgente` viraria 422 na listagem e derrubaria a tela
 * inteira; um `?competencia=2026-13` não derrubaria nada — `GET /api/documentos`
 * compara competência como texto e devolveria zero resultado — mas produziria
 * algo pior: uma tela dizendo "nenhum documento" enquanto o controle de
 * competência, que não sabe exibir mês 13, aparece vazio como se nada estivesse
 * filtrado. Valor que não reconhecemos vira filtro ausente, e aí a lista e os
 * controles contam a mesma história.
 */
export function lerFiltros(params: ParametrosDaUrl): FiltrosDeDocumentos {
  const competencia = primeiro(params.competencia);
  const status = primeiro(params.status);
  const operadoraId = primeiro(params.operadora_id);
  const offset = Number(primeiro(params.offset));

  return {
    competencia:
      competencia !== undefined && PADRAO_COMPETENCIA.test(competencia) ? competencia : undefined,
    status: STATUS_DE_DOCUMENTO.find((valido) => valido === status),
    operadora_id:
      operadoraId !== undefined && PADRAO_UUID.test(operadoraId) ? operadoraId : undefined,
    offset: Number.isSafeInteger(offset) && offset > 0 ? offset : 0,
  };
}

/** Há algum filtro em vigor? O `offset` não conta: paginar não é filtrar. */
export function temFiltro(filtros: FiltrosDeDocumentos): boolean {
  return (
    filtros.competencia !== undefined ||
    filtros.status !== undefined ||
    filtros.operadora_id !== undefined
  );
}

/**
 * A competência é o **único** filtro em vigor?
 *
 * Serve a uma distinção que a tela precisa fazer e a API não faz por ela: uma
 * competência sem nenhum documento não é a mesma coisa que um recorte que não
 * casou com nada. No primeiro caso o mês está vazio — mudar de mês é a saída;
 * no segundo, foi a combinação de filtros que excluiu tudo, e limpá-los é a
 * saída.
 */
export function apenasCompetenciaFiltrada(filtros: FiltrosDeDocumentos): boolean {
  return (
    filtros.competencia !== undefined &&
    filtros.status === undefined &&
    filtros.operadora_id === undefined
  );
}

/**
 * Os filtros no formato que a API espera.
 *
 * `paciente_id` existe no contrato e não é preenchido: esta tela não tem de
 * onde escolher um paciente, e nome de paciente nunca vira filtro de URL.
 */
export function filtrosDaApi(filtros: FiltrosDeDocumentos): ListarDocumentosParams {
  return {
    competencia: filtros.competencia,
    status: filtros.status,
    operadora_id: filtros.operadora_id,
  };
}

/**
 * O endereço da listagem com estes filtros — o único lugar que monta esta URL.
 *
 * Omite o que está vazio para a barra de endereços continuar legível, e omite
 * `offset=0` porque a primeira página é o default: sem isso, dois endereços
 * diferentes mostrariam a mesma tela.
 */
export function urlComFiltros(filtros: FiltrosDeDocumentos): string {
  const busca = new URLSearchParams();
  if (filtros.competencia !== undefined) busca.set("competencia", filtros.competencia);
  if (filtros.status !== undefined) busca.set("status", filtros.status);
  if (filtros.operadora_id !== undefined) busca.set("operadora_id", filtros.operadora_id);
  if (filtros.offset > 0) busca.set("offset", String(filtros.offset));

  const texto = busca.toString();
  return texto === "" ? CAMINHO_DOCUMENTOS : `${CAMINHO_DOCUMENTOS}?${texto}`;
}

/**
 * O endereço do detalhe de um documento.
 *
 * O id vai no **caminho**, nunca em query string, e nada mais vai junto: o
 * detalhe carrega prontuário, e um filtro carregado para dentro da URL do
 * documento acabaria em histórico e log de proxy sem precisar.
 */
export function urlDoDocumento(documentoId: string): string {
  return `${CAMINHO_DOCUMENTOS}/${encodeURIComponent(documentoId)}`;
}
