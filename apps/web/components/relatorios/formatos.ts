/**
 * Como os números e as datas do relatório são lidos e escritos.
 *
 * Módulo puro, sem React: as mesmas funções servem ao Server Component que
 * renderiza o painel e ao formulário de baseline, que precisa converter o que a
 * pessoa digitou **antes** de mandar para a API.
 *
 * Duas conversões aqui não são formatação, são contrato:
 *
 * - **taxa é razão 0..1**, não percentual. A API já arredonda em quatro casas
 *   (`reports/metricas.CASAS_TAXA`); multiplicar por 100 é trabalho da
 *   exibição, e `Intl` faz isso sozinho no estilo `percent`.
 * - **dinheiro é inteiro em centavos** no contrato inteiro
 *   (`valor_glosado_centavos`). Reais só existem na tela; a conversão acontece
 *   nas duas funções abaixo e em lugar nenhum mais.
 */

/**
 * Fuso fixo, e não o do servidor: o mesmo container roda em UTC em produção e
 * no fuso da máquina em desenvolvimento, e uma data que muda de dia conforme
 * onde o processo está hospedado discorda de si mesma entre ambientes. A
 * operação é brasileira; o horário é o dela.
 */
const FORMATO_DATA_HORA = new Intl.DateTimeFormat("pt-BR", {
  dateStyle: "short",
  timeStyle: "short",
  timeZone: "America/Sao_Paulo",
});

const FORMATO_COMPETENCIA = new Intl.DateTimeFormat("pt-BR", {
  month: "long",
  year: "numeric",
  timeZone: "UTC",
});

const FORMATO_PERCENTUAL = new Intl.NumberFormat("pt-BR", {
  style: "percent",
  minimumFractionDigits: 1,
  maximumFractionDigits: 2,
});

const FORMATO_INTEIRO = new Intl.NumberFormat("pt-BR");

const FORMATO_HORAS = new Intl.NumberFormat("pt-BR", {
  minimumFractionDigits: 1,
  maximumFractionDigits: 2,
});

/**
 * `variacao_pontos_percentuais` já vem multiplicada por 100 pela API
 * (`(final - inicial) * 100`) — é diferença de pontos percentuais, não uma
 * razão 0..1. Por isso não passa por `FORMATO_PERCENTUAL`: seria multiplicar
 * por 100 de novo.
 */
const FORMATO_PONTOS = new Intl.NumberFormat("pt-BR", {
  minimumFractionDigits: 1,
  maximumFractionDigits: 2,
});

const FORMATO_REAIS = new Intl.NumberFormat("pt-BR", {
  style: "currency",
  currency: "BRL",
});

/** Instante com data e hora (`recebido_em`, `deadline`, `updated_at`). */
export function formatarDataHora(iso: string): string {
  return FORMATO_DATA_HORA.format(new Date(iso));
}

/**
 * Data **sem hora** (`data_atendimento`, que a API serializa como `AAAA-MM-DD`),
 * reordenada como texto.
 *
 * Sem `Date` de propósito: `new Date("2026-08-01")` é interpretado como
 * meia-noite **UTC**, e formatá-lo em `America/Sao_Paulo` mostraria 31/07 — a
 * data de atendimento do prontuário andaria um dia para trás na tela inteira.
 * Uma data sem hora não tem fuso a converter.
 */
export function formatarDataIso(data: string): string {
  const partes = data.split("-");
  if (partes.length !== 3) {
    return data;
  }
  const [ano, mes, dia] = partes;
  return `${dia}/${mes}/${ano}`;
}

/** Competência `AAAA-MM` por extenso ("agosto de 2026"). */
export function formatarCompetencia(competencia: string): string {
  const partes = competencia.split("-");
  if (partes.length !== 2) {
    return competencia;
  }
  const data = new Date(Date.UTC(Number(partes[0]), Number(partes[1]) - 1, 1));
  if (Number.isNaN(data.getTime())) {
    return competencia;
  }
  return FORMATO_COMPETENCIA.format(data);
}

/** Razão 0..1 como percentual ("0.1234" → "12,34%"). */
export function formatarPercentual(taxa: number): string {
  return FORMATO_PERCENTUAL.format(taxa);
}

export function formatarInteiro(valor: number): string {
  return FORMATO_INTEIRO.format(valor);
}

export function formatarHoras(horas: number): string {
  return `${FORMATO_HORAS.format(horas)} h`;
}

/**
 * Magnitude de `variacao_pontos_percentuais`, em pontos percentuais — sem
 * sinal. O sinal é da API (negativo é queda de glosa, ou seja, melhora; ver
 * `PainelComparacaoGlosa`) e cabe a quem chama decidir o rótulo ("queda de" /
 * "alta de"), não a este formatador.
 */
export function formatarPontosPercentuais(valor: number): string {
  return `${FORMATO_PONTOS.format(Math.abs(valor))} p.p.`;
}

/**
 * Centavos inteiros em reais para exibição ("123456" → "R$ 1.234,56").
 *
 * A divisão por 100 acontece **aqui e só aqui**, na última linha antes da tela,
 * e sobre um inteiro que cabe folgadamente num `number`. Guardar o valor já
 * dividido em qualquer variável intermediária é o caminho conhecido para o erro
 * de 100x: alguém adiante trata reais como centavos, ou centavos como reais, e
 * os dois continuam sendo `number`.
 */
export function formatarReais(centavos: number): string {
  return FORMATO_REAIS.format(centavos / 100);
}

/**
 * O que a leitura de um campo de dinheiro pode produzir.
 *
 * `vazio` e `invalido` são estados diferentes porque exigem reações diferentes:
 * campo em branco é "não informar o valor" (a API aceita `null`), enquanto
 * `"12,345"` é erro de digitação que precisa ser mostrado. Um `number | null`
 * confundiria os dois e mandaria silenciosamente `null` para a API — o valor
 * que a pessoa digitou sumiria sem aviso.
 */
export type LeituraDeValor =
  | { tipo: "vazio" }
  | { tipo: "invalido"; motivo: string }
  | { tipo: "ok"; centavos: number };

const PADRAO_SO_DIGITOS = /^\d+$/;

/**
 * Lê um valor em **reais** digitado por uma pessoa e devolve **centavos
 * inteiros**, que é o que `valor_glosado_centavos` exige.
 *
 * A conversão é aritmética inteira do começo ao fim — `inteiro * 100 +
 * centavos` sobre as duas metades do texto — e nunca `Number(texto) * 100`.
 * Não é preciosismo: `19.99 * 100` é `1998.9999999999998` em ponto flutuante, e
 * o `Math.round` que conserta esse caso esconde o problema em vez de removê-lo.
 *
 * Aceita o que uma pessoa brasileira digita: `1.234,56`, `1234,56`, `1234.56`,
 * `1234`, com ou sem `R$`. A regra do ponto sozinho é a única ambígua e está
 * fixada aqui: ponto seguido de **exatamente três dígitos** é separador de
 * milhar (`1.234`), com uma ou duas casas é separador decimal (`1234.56`).
 * Mais de duas casas decimais é recusado — centavo tem duas casas, e arredondar
 * `12,345` em silêncio é exatamente o tipo de decisão que ninguém pediu.
 */
export function centavosDeReais(texto: string): LeituraDeValor {
  // `\s` cobre também o espaço inquebrável e o estreito que o próprio `Intl`
  // usa em pt-BR — que é o que chega quando alguém copia um valor já
  // formatado de volta para o campo.
  const limpo = texto.replace(/\s/g, "").replace(/^R\$/i, "");
  if (limpo === "") {
    return { tipo: "vazio" };
  }

  let normalizado = limpo;
  if (limpo.includes(",")) {
    // Com vírgula presente, o ponto só pode ser separador de milhar.
    normalizado = limpo.replace(/\./g, "").replace(",", ".");
  } else {
    const pontos = limpo.split(".");
    const ultimo = pontos[pontos.length - 1];
    if (pontos.length > 1 && ultimo.length === 3) {
      normalizado = pontos.join("");
    }
  }

  const partes = normalizado.split(".");
  if (partes.length > 2) {
    return { tipo: "invalido", motivo: "Valor inválido. Use o formato 1.234,56." };
  }

  const [inteiro, decimais = ""] = partes;
  if (!PADRAO_SO_DIGITOS.test(inteiro) || (decimais !== "" && !PADRAO_SO_DIGITOS.test(decimais))) {
    return { tipo: "invalido", motivo: "Valor inválido. Use apenas números, como 1.234,56." };
  }
  if (decimais.length > 2) {
    return {
      tipo: "invalido",
      motivo: "Centavos têm no máximo duas casas decimais (por exemplo, 1.234,56).",
    };
  }

  const centavos = Number(inteiro) * 100 + Number(decimais.padEnd(2, "0"));
  if (!Number.isSafeInteger(centavos)) {
    return { tipo: "invalido", motivo: "Valor alto demais para ser registrado." };
  }
  return { tipo: "ok", centavos };
}

/**
 * As descrições de `problema_encontrado`, que a API entrega unidas por `" | "`.
 *
 * `""` vira lista vazia, e **não** um item em branco: string vazia é "nenhuma
 * pendência aberta", e uma linha vazia na tela pareceria um problema sem
 * descrição.
 */
export function problemasDaLinha(problemaEncontrado: string): string[] {
  if (problemaEncontrado === "") {
    return [];
  }
  return problemaEncontrado.split(" | ");
}

/**
 * Como o documento é referenciado na tela.
 *
 * Oito caracteres bastam para separar as linhas da mesma página; o id inteiro
 * fica no `title`, para copiar quando for preciso.
 */
export function referenciaDoDocumento(documentoId: string): string {
  return documentoId.slice(0, 8);
}
