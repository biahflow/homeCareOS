import type { CanalAlerta, StatusAlerta, TipoAlerta } from "@homecareos/contracts";

/**
 * Os filtros do log de alertas vivem na **URL**, não em estado de cliente — o
 * mesmo que a administração de usuários, a fila de pendências e a listagem de
 * documentos fazem, e pelas mesmas três razões: a listagem fica compartilhável
 * ("olha este alerta aqui"), sobrevive a uma recarga no meio do turno, e o
 * botão "voltar" do navegador desfaz o último filtro em vez de sair da tela.
 *
 * Módulo puro de propósito — sem `next/headers`, sem hooks: é a mesma fonte de
 * verdade para o Server Component que lê a URL e para o componente de filtros
 * que a reescreve.
 */

export const CAMINHO_ALERTAS = "/alertas";

/**
 * Itens por página.
 *
 * Abaixo do padrão da API (50) e igual ao das outras listagens: cada linha
 * carrega o texto da mensagem enviada, que é mais longo que uma linha de
 * cadastro — cinquenta delas empurrariam a paginação para fora da tela.
 */
export const LIMITE_POR_PAGINA = 25;

/** Os quatro tipos de alerta da issue #9, na ordem do `README` da API. */
export const TIPOS_DE_ALERTA: readonly TipoAlerta[] = [
  "documento_incompleto_critico",
  "deadline_competencia",
  "volume_anormal",
  "pendencia_parada",
];

export const ROTULO_DE_TIPO: Record<TipoAlerta, string> = {
  documento_incompleto_critico: "Documento incompleto crítico",
  deadline_competencia: "Prazo de competência",
  volume_anormal: "Volume anormal",
  pendencia_parada: "Pendência parada",
};

/** Os três desfechos que uma tentativa de notificação grava. */
export const STATUS_DE_ALERTA: readonly StatusAlerta[] = ["enviado", "falha", "suprimido"];

export const ROTULO_DE_STATUS: Record<StatusAlerta, string> = {
  enviado: "Enviado",
  falha: "Falha",
  suprimido: "Suprimido",
};

/**
 * Variante do selo `.state` por status. O mapa devolve a variante, nunca a
 * cor: `enviado` é o desfecho bom (verde), `suprimido` é o alerta que merece
 * atenção mas não é falha (âmbar), `falha` é o problema de verdade (vermelho).
 */
export const VARIANTE_DE_STATUS: Record<StatusAlerta, string> = {
  enviado: "state--1",
  suprimido: "state--2",
  falha: "state--3",
};

/*
 * Os três acessos abaixo existem porque **o tipo mente sobre o dado**.
 *
 * `AlertaItem.tipo` e `.status` são uniões de string literal no TypeScript,
 * mas a API os declara como `str` puro (`alerts/router.py`, `AlertaItem`): o
 * fechamento é `enum.StrEnum` só na escrita, não na resposta. Um detector novo
 * no backend — que é exatamente o tipo de coisa que se acrescenta sem tocar no
 * front — chegaria aqui como um valor que nenhum dos mapas conhece, e um
 * `Record` indexado por chave ausente devolve `undefined`.
 *
 * Sem estas funções, o efeito seria o pior possível numa tela de auditoria: o
 * nome do alerta simplesmente **some**, e a linha continua ali parecendo
 * completa. Mostrar o valor cru é feio e é honesto — quem vê
 * `documento_incompleto_v2` sabe que existe algo novo; quem vê um espaço vazio
 * não sabe de nada.
 */

export const ROTULO_DE_CANAL: Record<CanalAlerta, string> = {
  whatsapp: "WhatsApp",
  email: "E-mail",
};

/** Mesmo fallback dos outros rótulos: canal novo aparece cru, não some. */
export function rotuloDoCanal(canal: string): string {
  return ROTULO_DE_CANAL[canal as CanalAlerta] ?? canal;
}

export function rotuloDoTipo(tipo: string): string {
  return ROTULO_DE_TIPO[tipo as TipoAlerta] ?? tipo;
}

export function rotuloDoStatus(status: string): string {
  return ROTULO_DE_STATUS[status as StatusAlerta] ?? status;
}

/** Cinza para status desconhecido: não afirma nem sucesso nem falha. */
export function varianteDoStatus(status: string): string {
  return VARIANTE_DE_STATUS[status as StatusAlerta] ?? "state--off";
}

export interface FiltrosDeAlertas {
  tipo?: TipoAlerta;
  status?: StatusAlerta;
  /**
   * Filtra pelo documento a que o alerta se refere. Sem controle próprio nesta
   * tela — não há uma lista de documentos aqui para escolher de um seletor —,
   * mas suportado na URL para um link futuro (ex.: "ver alertas deste
   * documento" na tela de detalhe) poder apontar direto para o recorte certo.
   */
  documento_id?: string;
  offset: number;
}

type ParametrosDaUrl = Record<string, string | string[] | undefined>;

const PADRAO_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** `?a=1&a=2` chega como array; o primeiro valor é o que vale. */
function primeiro(valor: string | string[] | undefined): string | undefined {
  return Array.isArray(valor) ? valor[0] : valor;
}

/**
 * Traduz a query string em filtros, **descartando o que a API recusaria**.
 *
 * A query string é entrada de fora: qualquer link colado pode conter qualquer
 * coisa, e um `?status=talvez` viraria 422 na listagem e derrubaria a tela
 * inteira. Valor que não reconhecemos é tratado como filtro ausente — a lista
 * mostra tudo e o controle mostra "Todos", que é o estado que ela de fato está
 * exibindo.
 */
export function lerFiltros(params: ParametrosDaUrl): FiltrosDeAlertas {
  const tipo = primeiro(params.tipo);
  const status = primeiro(params.status);
  const documentoId = primeiro(params.documento_id);
  const offset = Number(primeiro(params.offset));

  return {
    tipo: TIPOS_DE_ALERTA.find((valido) => valido === tipo),
    status: STATUS_DE_ALERTA.find((valido) => valido === status),
    documento_id:
      documentoId !== undefined && PADRAO_UUID.test(documentoId) ? documentoId : undefined,
    offset: Number.isSafeInteger(offset) && offset > 0 ? offset : 0,
  };
}

/** Há algum filtro em vigor? O `offset` não conta: paginar não é filtrar. */
export function temFiltro(filtros: FiltrosDeAlertas): boolean {
  return (
    filtros.tipo !== undefined ||
    filtros.status !== undefined ||
    filtros.documento_id !== undefined
  );
}

/**
 * O endereço do log com estes filtros — o único lugar que monta esta URL.
 *
 * Omite o que está vazio para a barra de endereços continuar legível, e omite
 * `offset=0` porque a primeira página é o default: sem isso, dois endereços
 * diferentes mostrariam a mesma tela.
 */
export function urlComFiltros(filtros: FiltrosDeAlertas): string {
  const busca = new URLSearchParams();
  if (filtros.tipo !== undefined) busca.set("tipo", filtros.tipo);
  if (filtros.status !== undefined) busca.set("status", filtros.status);
  if (filtros.documento_id !== undefined) busca.set("documento_id", filtros.documento_id);
  if (filtros.offset > 0) busca.set("offset", String(filtros.offset));

  const texto = busca.toString();
  return texto === "" ? CAMINHO_ALERTAS : `${CAMINHO_ALERTAS}?${texto}`;
}
