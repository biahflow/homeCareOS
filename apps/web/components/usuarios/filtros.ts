import type { Papel } from "@homecareos/contracts";

/**
 * Os filtros da administração de usuários vivem na **URL**, não em estado de
 * cliente — o mesmo que a fila de pendências, a listagem de documentos e o
 * relatório fazem, e pelas mesmas três razões: a listagem fica compartilhável
 * ("olha esta pessoa aqui"), sobrevive a uma recarga no meio do turno, e o
 * botão "voltar" do navegador desfaz o último filtro em vez de sair da tela.
 *
 * Aqui há uma quarta razão, específica desta tela: criar e editar recarregam a
 * lista do servidor (`router.refresh()`), e ela precisa voltar com o mesmo
 * recorte que estava na tela. Com o filtro em estado de cliente, o refresh o
 * perderia.
 *
 * Módulo puro de propósito — sem `next/headers`, sem hooks: é a mesma fonte de
 * verdade para o Server Component que lê a URL e para o componente de filtros
 * que a reescreve.
 */

export const CAMINHO_USUARIOS = "/usuarios";

/**
 * Itens por página.
 *
 * Abaixo do padrão da API (50) e igual ao das outras listagens: a página também
 * carrega o formulário de criação, e cinquenta linhas o empurrariam para fora
 * da tela.
 */
export const LIMITE_POR_PAGINA = 25;

/**
 * Os papéis que **esta API atribui** — `usuarios_router.PAPEIS_ATRIBUIVEIS`.
 *
 * `gestor` fica de fora, e não é omissão de interface: a API responde 403 a
 * quem o pede, porque um coordenador que criasse um gestor estaria se dando
 * acesso a dado de gestão que o papel dele não tem (ADR 0001 e ADR 0004).
 * Criar gestor é `python -m homecareos.auth.cli criar`, no servidor.
 *
 * Oferecer uma opção que só existe para ser recusada faria a pessoa descobrir a
 * regra pelo erro. Isto **não** substitui o tratamento do 403: a lista de
 * papéis atribuíveis é do servidor e pode mudar sem esta tela saber.
 */
export const PAPEIS_ATRIBUIVEIS: readonly Papel[] = ["conferente", "coordenador"];

/**
 * A situação de uma conta, como esta tela a nomeia.
 *
 * `ativo` é **três estados na API** (`true`, `false`, ausente = todos) e dois na
 * linha da lista. Os nomes existem para os dois lados não se confundirem: o
 * filtro escolhe entre três, a conta está em um de dois.
 */
export const ROTULO_DE_SITUACAO = {
  ativo: "Ativo",
  desativado: "Desativado",
} as const;

export interface FiltrosDeUsuarios {
  /** `true` só ativos, `false` só desativados, ausente todos. */
  ativo?: boolean;
  offset: number;
}

type ParametrosDaUrl = Record<string, string | string[] | undefined>;

/** `?a=1&a=2` chega como array; o primeiro valor é o que vale. */
function primeiro(valor: string | string[] | undefined): string | undefined {
  return Array.isArray(valor) ? valor[0] : valor;
}

/**
 * Traduz a query string em filtros, **descartando o que a API recusaria**.
 *
 * A query string é entrada de fora: qualquer link colado pode conter qualquer
 * coisa, e um `?ativo=talvez` viraria 422 na listagem e derrubaria a tela
 * inteira. Valor que não reconhecemos é tratado como filtro ausente — a lista
 * mostra todos e o controle mostra "Todos", que é o estado que ela de fato
 * está exibindo.
 *
 * `ativo` é lido como as duas strings exatas que a URL desta tela escreve, e
 * não por "qualquer coisa diferente de `false` é `true`": o segundo aceitaria
 * `?ativo=0` como "só ativos" e mostraria o oposto do que a pessoa pediu.
 */
export function lerFiltros(params: ParametrosDaUrl): FiltrosDeUsuarios {
  const ativo = primeiro(params.ativo);
  const offset = Number(primeiro(params.offset));

  return {
    ativo: ativo === "true" ? true : ativo === "false" ? false : undefined,
    offset: Number.isSafeInteger(offset) && offset > 0 ? offset : 0,
  };
}

/** Há algum filtro em vigor? O `offset` não conta: paginar não é filtrar. */
export function temFiltro(filtros: FiltrosDeUsuarios): boolean {
  return filtros.ativo !== undefined;
}

/**
 * O endereço da tela com estes filtros — o único lugar que monta esta URL.
 *
 * Omite o que está vazio para a barra de endereços continuar legível, e omite
 * `offset=0` porque a primeira página é o default: sem isso, dois endereços
 * diferentes mostrariam a mesma tela.
 */
export function urlComFiltros(filtros: FiltrosDeUsuarios): string {
  const busca = new URLSearchParams();
  if (filtros.ativo !== undefined) busca.set("ativo", String(filtros.ativo));
  if (filtros.offset > 0) busca.set("offset", String(filtros.offset));

  const texto = busca.toString();
  return texto === "" ? CAMINHO_USUARIOS : `${CAMINHO_USUARIOS}?${texto}`;
}
