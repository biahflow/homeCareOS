import type { ExtracaoResumo } from "@homecareos/contracts";

/**
 * Como a extração é lida na tela — e por que ela não pode derrubar a tela.
 *
 * `campos_extraidos` e `confianca_por_campo` são `dict[str, Any]` na API: não
 * há schema fechado no contrato HTTP, e o schema de extração
 * (`extraction/schema.py:EvolucaoProntuario`) muda sem versionar a rota. Todo
 * valor chega como `unknown` e é checado antes de virar texto. É o ponto do
 * módulo: **a página que existe para revelar que a extração saiu ruim não pode
 * quebrar porque a extração saiu ruim** — campo a mais, campo a menos, tipo
 * inesperado, mapa de confiança vazio, tudo isso é conteúdo a mostrar, não
 * exceção a lançar.
 *
 * Módulo puro, sem React: as mesmas funções servem ao Server Component do
 * detalhe e a qualquer outra tela que venha a mostrar extração.
 */

/**
 * A confiança **nunca arredonda para cima**.
 *
 * `maximumFractionDigits: 1` com o arredondamento padrão transformaria 0,9996
 * em "100,0%" — a tela afirmando leitura perfeita de um campo que o modelo não
 * leu perfeitamente, exatamente a mentira que mostrar confiança existe para
 * evitar. `roundingMode: "floor"` trunca: 100% só aparece quando o valor é 100%.
 */
const FORMATO_CONFIANCA = new Intl.NumberFormat("pt-BR", {
  style: "percent",
  minimumFractionDigits: 0,
  maximumFractionDigits: 1,
  roundingMode: "floor",
});

/** Um campo da extração, pronto para a tela. */
export interface CampoExtraido {
  /**
   * A chave como a API a devolveu (`carimbo_legivel`), sem embelezar.
   *
   * É de propósito: as validações nomeiam o campo por esta chave ("Campo
   * 'carimbo_legivel' foi marcado como ilegível pela extração"), e um rótulo
   * bonito quebraria a correspondência entre o que a validação reclama e o que
   * a extração mostra. Fora que o schema é aberto — um campo novo não teria
   * rótulo nenhum e apareceria em branco.
   */
  nome: string;
  /** O valor, já convertido para texto legível. */
  valor: string;
  /** A extração não trouxe conteúdo para este campo (nulo, vazio, lista vazia). */
  semConteudo: boolean;
  /** 0..1, ou `null` quando a extração não mediu a confiança deste campo. */
  confianca: number | null;
}

/** Trata como registro só o que de fato é um objeto — `null` e array não são. */
function registro(valor: unknown): Record<string, unknown> {
  if (typeof valor !== "object" || valor === null || Array.isArray(valor)) {
    return {};
  }
  return valor as Record<string, unknown>;
}

/** Texto curto para um item de lista. Não recursivo: lista de lista vira JSON. */
function itemComoTexto(item: unknown): string {
  if (item === null || item === undefined) return "—";
  if (typeof item === "string") return item;
  if (typeof item === "number") return String(item);
  if (typeof item === "boolean") return item ? "sim" : "não";
  try {
    return JSON.stringify(item) ?? "—";
  } catch {
    return "(valor não legível)";
  }
}

/**
 * Converte um valor da extração em texto, dizendo também se ele está vazio.
 *
 * "Vazio" e "não extraído" são a mesma coisa aqui — o schema da extração
 * registra o que não foi lido como `null` ou lista vazia — e são diferentes de
 * um valor presente: a tela precisa distinguir "o modelo não leu" de "o modelo
 * leu e o campo é falso", porque `carimbo_presente: false` é uma afirmação, não
 * uma ausência.
 */
export function valorComoTexto(valor: unknown): { texto: string; semConteudo: boolean } {
  if (valor === null || valor === undefined) {
    return { texto: "não extraído", semConteudo: true };
  }
  if (typeof valor === "string") {
    return valor.trim() === ""
      ? { texto: "não extraído", semConteudo: true }
      : { texto: valor, semConteudo: false };
  }
  if (typeof valor === "boolean") {
    return { texto: valor ? "sim" : "não", semConteudo: false };
  }
  if (typeof valor === "number") {
    return { texto: String(valor), semConteudo: false };
  }
  if (Array.isArray(valor)) {
    return valor.length === 0
      ? { texto: "nenhum", semConteudo: true }
      : { texto: valor.map(itemComoTexto).join(" · "), semConteudo: false };
  }
  try {
    return { texto: JSON.stringify(valor) ?? "(valor não legível)", semConteudo: false };
  } catch {
    return { texto: "(valor não legível)", semConteudo: false };
  }
}

/** A confiança de um campo, ou `null` quando não veio como número. */
export function confiancaDeCampo(
  confiancaPorCampo: Record<string, unknown>,
  nome: string,
): number | null {
  const valor = registro(confiancaPorCampo)[nome];
  return typeof valor === "number" && Number.isFinite(valor) ? valor : null;
}

/**
 * Os campos da extração, **do menos lido para o mais lido**.
 *
 * A ordem é o ponto: quem confere abre esta tela para achar o que o sistema não
 * conseguiu ler, e é isso que precisa estar no topo. A ordem do schema
 * colocaria `nome_paciente` primeiro e o campo ilegível no meio da lista.
 *
 * As chaves saem da **união** de `campos_extraidos` e `confianca_por_campo`.
 * Percorrer só a primeira sumiria com um campo que tem confiança medida e não
 * veio no payload — e sumir com o campo de confiança baixa é o único erro que
 * esta tela não pode cometer.
 */
export function camposDaExtracao(extracao: ExtracaoResumo): CampoExtraido[] {
  const campos = registro(extracao.campos_extraidos);
  const confiancas = registro(extracao.confianca_por_campo);
  const nomes = [...new Set([...Object.keys(campos), ...Object.keys(confiancas)])];

  return nomes
    .map((nome) => {
      const { texto, semConteudo } = valorComoTexto(campos[nome]);
      return {
        nome,
        valor: texto,
        semConteudo,
        confianca: confiancaDeCampo(confiancas, nome),
      };
    })
    .sort((a, b) => {
      // Campo sem confiança medida vai para o fim: não é "confiança alta", é
      // "não medida", e misturá-lo com os medidos esconderia os baixos.
      if (a.confianca === null && b.confianca === null) return a.nome.localeCompare(b.nome, "pt-BR");
      if (a.confianca === null) return 1;
      if (b.confianca === null) return -1;
      if (a.confianca !== b.confianca) return a.confianca - b.confianca;
      return a.nome.localeCompare(b.nome, "pt-BR");
    });
}

/** Razão 0..1 como percentual, truncada para baixo ("0.9996" → "99,9%"). */
export function formatarConfianca(valor: number): string {
  return FORMATO_CONFIANCA.format(valor);
}

/**
 * O que a confiança quer dizer, em palavras.
 *
 * O provider produz **três níveis**, e eles são o que a faixa abaixo nomeia
 * (`extraction/claude.py:_confianca`): `0.0` para campo que o próprio modelo
 * listou como ilegível, `0.5` para campo lido com dúvida (`campos_incertos`) e
 * `1.0` para o resto. As comparações são por faixa, e não por igualdade exata,
 * porque a API tipa o valor como `Any`: um número fora dos três níveis precisa
 * receber um nome honesto em vez de cair em "outro" ou quebrar a linha.
 *
 * **O rótulo nunca substitui o número** — quem exibe mostra os dois. É a
 * diferença entre informar a incerteza e traduzi-la para uma palavra que a
 * arredonda.
 */
export function rotuloDeConfianca(valor: number): string {
  if (valor <= 0) return "não lido";
  if (valor < 1) return "lido com dúvida";
  return "lido";
}

/**
 * Variante do selo `.state` para uma confiança.
 *
 * Não confundir com o mapa de status (`vocabulario.ts`): aqui a entrada é um
 * número que a API mediu, não um estado de documento, e a cor acompanha o
 * número que está escrito ao lado — ela destaca, nunca substitui.
 */
export function varianteDeConfianca(valor: number): string {
  if (valor <= 0) return "state--3";
  if (valor < 1) return "state--2";
  return "state--1";
}
