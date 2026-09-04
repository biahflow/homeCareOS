import type { DocumentoStatus, Severidade, TipoDocumento } from "@homecareos/contracts";

/**
 * O vocabulário do documento em `apps/web`: como o status e o tipo se escrevem,
 * em que ordem os status vêm e **de onde sai a cor**.
 *
 * Módulo puro de propósito — sem React, sem `next/headers`: é a mesma fonte de
 * verdade para a listagem de documentos, para o detalhe e para o relatório de
 * conferência (`components/relatorios/filtros.ts` reexporta daqui em vez de
 * manter uma segunda cópia).
 */

/** Os sete status do documento, na ordem em que o ciclo os percorre. */
export const STATUS_DE_DOCUMENTO: readonly DocumentoStatus[] = [
  "processando",
  "aprovado",
  "problema",
  "incompleto",
  "em_correcao",
  "resolvido",
  "liberado",
];

/**
 * Como cada status é escrito em português.
 *
 * É **rótulo**, não severidade: traduzir o nome do status não recria nenhuma
 * regra de produto. Quem decide gravidade é `SEVERIDADE_POR_STATUS`, abaixo.
 */
export const ROTULO_DE_STATUS_DOCUMENTO: Record<DocumentoStatus, string> = {
  processando: "Processando",
  aprovado: "Aprovado",
  problema: "Problema",
  incompleto: "Incompleto",
  em_correcao: "Em correção",
  resolvido: "Resolvido",
  liberado: "Liberado",
};

/** Como cada tipo de documento é escrito (`db/models/enums.py:TipoDocumento`). */
export const ROTULO_DE_TIPO_DOCUMENTO: Record<TipoDocumento, string> = {
  evolucao: "Evolução",
  ficha_visita: "Ficha de visita",
  boletim: "Boletim",
  matmed: "MatMed",
};

/**
 * Variante do selo `.state` por **severidade**. O mapa devolve a variante,
 * nunca a cor.
 *
 * Este é o **único** lugar de `apps/web` que decide a aparência de um estado de
 * documento. O relatório de conferência entra aqui direto, com a `severidade`
 * que a API respondeu; a listagem de documentos entra pela ponte abaixo.
 */
export const VARIANTE_DE_SEVERIDADE: Record<Severidade, string> = {
  CRITICO: "state--3",
  ATENCAO: "state--2",
  OK: "state--1",
};

/**
 * A severidade de cada status — **uma cópia deliberada de uma regra que mora no
 * servidor**, e a única de `apps/web`.
 *
 * "Incompleto é crítico, problema é atenção, aprovado é OK" é decisão de
 * produto, vive em `reports/conferencia._SEVERIDADE_POR_STATUS` e é testada lá.
 * `GET /api/relatorios/conferencia` a expõe pronta, no campo `severidade` de
 * cada linha, justamente para o cliente não recriá-la — mas
 * **`GET /api/documentos` não expõe severidade nenhuma**, e o documento
 * precisa da mesma cor nas duas telas: o mesmo documento vermelho no relatório
 * e amarelo na listagem seria o front contando duas histórias sobre o mesmo
 * dado.
 *
 * Então a duplicação aqui é **forçada pelo contrato**, não descuido — e é uma
 * dívida com endereço: a saída definitiva é `/api/documentos` passar a
 * devolver `severidade` como o relatório já faz. No dia em que devolver, some
 * este mapa e a listagem usa o campo da resposta; `VARIANTE_DE_SEVERIDADE`
 * acima continua igual, porque a cor nunca foi decidida aqui.
 *
 * Enquanto isso: qualquer mudança em `_SEVERIDADE_POR_STATUS` precisa ser
 * repetida neste objeto. Não há teste que ligue os dois.
 */
export const SEVERIDADE_POR_STATUS: Record<DocumentoStatus, Severidade> = {
  incompleto: "CRITICO",
  problema: "ATENCAO",
  em_correcao: "ATENCAO",
  resolvido: "ATENCAO",
  aprovado: "OK",
  liberado: "OK",
  processando: "OK",
};

/** A variante do selo de um status, pela severidade que a API atribui a ele. */
export function varianteDeStatus(status: DocumentoStatus): string {
  return VARIANTE_DE_SEVERIDADE[SEVERIDADE_POR_STATUS[status]];
}
