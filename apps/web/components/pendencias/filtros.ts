import type { PendenciaStatus } from "@homecareos/contracts";

/**
 * Os filtros da fila de pendências vivem na **URL**, não em estado de cliente.
 *
 * A consequência é o motivo: a listagem fica compartilhável ("olha esta fila
 * aqui"), sobrevive a uma recarga no meio do turno e o botão "voltar" do
 * navegador desfaz o último filtro em vez de sair da tela. Estado de cliente
 * não faz nenhuma das três, e a terceira é a que a pessoa tenta por instinto.
 *
 * Este módulo é puro de propósito — sem `next/headers`, sem hooks: ele é a
 * mesma fonte de verdade para o Server Component que lê a URL e para o
 * componente de filtros que a reescreve.
 */

export const CAMINHO_PENDENCIAS = "/pendencias";

/**
 * Itens por página.
 *
 * Abaixo do padrão da API (50) porque a linha desta lista não é uma linha: são
 * três, mais a ação. Cinquenta delas viram uma parede que ninguém varre até o
 * fim, e a paginação existe justamente para isso.
 */
export const LIMITE_POR_PAGINA = 25;

export interface FiltrosDePendencias {
  status?: PendenciaStatus;
  operadora_id?: string;
  /** Data `AAAA-MM-DD`: pendências com deadline até ela, dia inteiro incluído. */
  deadline?: string;
  offset: number;
}

/** Os três status do ciclo, na ordem em que a pendência os percorre. */
export const STATUS_DE_PENDENCIA: readonly PendenciaStatus[] = [
  "aberta",
  "em_correcao",
  "resolvida",
];

export const ROTULO_DE_STATUS: Record<PendenciaStatus, string> = {
  aberta: "Aberta",
  em_correcao: "Em correção",
  resolvida: "Resolvida",
};

/** Variante do selo `.state` por status. O mapa devolve a variante, nunca a cor. */
export const VARIANTE_DE_STATUS: Record<PendenciaStatus, string> = {
  aberta: "state--3",
  em_correcao: "state--2",
  resolvida: "state--1",
};

/**
 * A próxima etapa do ciclo, ou `null` quando não há para onde ir.
 *
 * `resolvida` termina o ciclo: a API não tem transição de volta
 * (`_TRANSICOES_VALIDAS`), e é isso que faz a ação sumir em vez de aparecer
 * desabilitada — botão desabilitado promete que algo destrava, e aqui não
 * destrava.
 */
export function proximoStatus(atual: PendenciaStatus): PendenciaStatus | null {
  if (atual === "aberta") return "em_correcao";
  if (atual === "em_correcao") return "resolvida";
  return null;
}

type ParametrosDaUrl = Record<string, string | string[] | undefined>;

const PADRAO_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PADRAO_DATA = /^\d{4}-\d{2}-\d{2}$/;

/** `?a=1&a=2` chega como array; o primeiro valor é o que vale. */
function primeiro(valor: string | string[] | undefined): string | undefined {
  return Array.isArray(valor) ? valor[0] : valor;
}

/**
 * Traduz a query string em filtros, **descartando o que a API recusaria**.
 *
 * Sem esta validação um `?status=urgente` digitado à mão viraria 422 na
 * listagem e derrubaria a tela inteira num erro de servidor — a query string é
 * entrada de fora, e qualquer link colado pode conter qualquer coisa. Valor que
 * não reconhecemos é tratado como filtro ausente: a tela continua de pé e os
 * controles mostram "todas", que é o estado que ela de fato está exibindo.
 */
export function lerFiltros(params: ParametrosDaUrl): FiltrosDePendencias {
  const status = primeiro(params.status);
  const operadoraId = primeiro(params.operadora_id);
  const deadline = primeiro(params.deadline);
  const offset = Number(primeiro(params.offset));

  return {
    status: STATUS_DE_PENDENCIA.find((valido) => valido === status),
    operadora_id: operadoraId !== undefined && PADRAO_UUID.test(operadoraId) ? operadoraId : undefined,
    deadline: deadline !== undefined && PADRAO_DATA.test(deadline) ? deadline : undefined,
    offset: Number.isSafeInteger(offset) && offset > 0 ? offset : 0,
  };
}

/** Há algum filtro em vigor? O `offset` não conta: paginar não é filtrar. */
export function temFiltro(filtros: FiltrosDePendencias): boolean {
  return (
    filtros.status !== undefined ||
    filtros.operadora_id !== undefined ||
    filtros.deadline !== undefined
  );
}

/**
 * O endereço da fila com estes filtros — o único lugar que monta esta URL.
 *
 * Omite o que está vazio para a barra de endereços continuar legível, e omite
 * `offset=0` porque a primeira página é o default: sem isso, dois endereços
 * diferentes mostrariam a mesma tela.
 */
export function urlComFiltros(filtros: FiltrosDePendencias): string {
  const busca = new URLSearchParams();
  if (filtros.status !== undefined) busca.set("status", filtros.status);
  if (filtros.operadora_id !== undefined) busca.set("operadora_id", filtros.operadora_id);
  if (filtros.deadline !== undefined) busca.set("deadline", filtros.deadline);
  if (filtros.offset > 0) busca.set("offset", String(filtros.offset));

  const texto = busca.toString();
  return texto === "" ? CAMINHO_PENDENCIAS : `${CAMINHO_PENDENCIAS}?${texto}`;
}
