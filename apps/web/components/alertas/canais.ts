import type { CanalAlerta, CanalOut } from "@homecareos/contracts";
import { ehAtorMaquina } from "@homecareos/contracts";
// Mesmo `Intl` configurado com o fuso da operação que o resto do app usa —
// duas configurações divergiriam na primeira mudança.
import { formatarDataHora } from "@/components/relatorios/formatos";

/**
 * O vocabulário da tela de canais de alerta (ADR 0006, parte 2b).
 *
 * Módulo puro de propósito — sem `next/headers`, sem hooks: é a mesma fonte de
 * verdade para o Server Component que desenha o estado e para o Client
 * Component que liga e desliga, e a decisão "isto silencia tudo?" precisa ser a
 * mesma nos dois.
 *
 * A regra que este módulo existe para não deixar ninguém quebrar:
 *
 * ```
 * canal habilitado (banco)  ×  credencial presente (.env)  =  canal envia
 * ```
 *
 * As duas perguntas aparecem separadas na tela porque juntá-las apaga a
 * diferença entre "desliguei" e "esqueci de configurar" — que é exatamente o
 * que o ADR 0006 manda a interface mostrar, "sob pena de alguém ligar um canal
 * e não entender por que nada sai".
 */

/** O endereço da tela. Sub-rota de `/alertas`: ver a docstring da página. */
export const CAMINHO_CANAIS = "/alertas/canais";

/**
 * Eventos de histórico por página.
 *
 * Bem abaixo do padrão da API (50) e das outras listagens (25): mudança de
 * canal é evento raro — dois canais, e cada linha é uma frase curta. Vinte
 * cobre o histórico inteiro de uma instalação por muito tempo, e a paginação
 * existe para o dia em que não cobrir.
 */
export const LIMITE_HISTORICO = 20;

/**
 * O que falta no `.env` para cada canal enviar, em nome de gente.
 *
 * Nomeia a variável junto do serviço porque quem lê esta tela (o coordenador)
 * não é quem resolve: ele repassa a alguém com acesso ao servidor, e "falta
 * SMTP" sem o nome do campo vira uma segunda pergunta. Os nomes saem de
 * `alerts/provider.get_provider` e `mailer/provider.get_email_provider`, que
 * são quem decide `disponivel`.
 */
const CREDENCIAL_DE_CANAL: Record<CanalAlerta, string> = {
  whatsapp: "as credenciais da uazapi (UAZAPI_BASE_URL e UAZAPI_TOKEN)",
  email: "as credenciais de SMTP (SMTP_HOST e SMTP_REMETENTE)",
};

/**
 * A credencial que falta, ou `undefined` para um canal que esta versão da
 * interface não conhece.
 *
 * Mesmo fallback de `rotuloDoCanal`, e pela mesma razão: um canal novo na API
 * chega aqui como um valor que este mapa não tem, e `Record` indexado por chave
 * ausente devolve `undefined`. Numa tela sobre por que um aviso não saiu, uma
 * frase que some em silêncio é o pior desfecho — quem chama escreve a versão
 * genérica em vez de escrever nada.
 */
export function credencialDoCanal(canal: string): string | undefined {
  return CREDENCIAL_DE_CANAL[canal as CanalAlerta];
}

/**
 * Os três estados que a tela nomeia, derivados dos **dois** booleanos.
 *
 * `desligado` não distingue disponível de indisponível de propósito: com o
 * canal desligado nada sai de qualquer jeito, e o selo estaria afirmando duas
 * coisas ao mesmo tempo. A credencial ausente de um canal desligado continua
 * dita — na frase de estado, que é onde ela muda o que a pessoa deve fazer.
 */
export type EstadoDoCanal = "enviando" | "ligado-sem-credencial" | "desligado";

export function estadoDoCanal(canal: CanalOut): EstadoDoCanal {
  if (!canal.habilitado) {
    return "desligado";
  }
  return canal.disponivel ? "enviando" : "ligado-sem-credencial";
}

/**
 * Selo por estado. O mapa devolve a variante, nunca a cor — mesma disciplina de
 * `VARIANTE_DE_STATUS` no log de alertas.
 *
 * `ligado-sem-credencial` é vermelho, e não âmbar: alguém decidiu que este
 * canal deve avisar e ele não avisa. É o estado que o ADR 0006 escreveu esta
 * tela para tornar visível, não um aviso menor.
 */
export const SELO_DE_ESTADO: Record<EstadoDoCanal, { rotulo: string; variante: string }> = {
  enviando: { rotulo: "Enviando", variante: "state--1" },
  "ligado-sem-credencial": { rotulo: "Ligado, sem enviar", variante: "state--3" },
  desligado: { rotulo: "Desligado", variante: "state--off" },
};

/**
 * De onde veio o estado atual deste canal.
 *
 * `herdada` é o caso que a tela **não pode** apresentar como decisão de alguém:
 * `atualizado_por` nulo é o valor semeado pela migração de configuração, e a
 * API o deixou nulo de propósito. Inventar "sistema" ou "automático" aqui faria
 * a tela mentir justamente no campo que existe para responder "quem silenciou a
 * operação?".
 *
 * Note que `herdada` vale tanto para canal ligado quanto desligado: hoje o
 * WhatsApp nasce ligado pela migração, e não há a quem creditar o "ligou".
 */
export type ProcedenciaDoCanal =
  | { tipo: "herdada" }
  | { tipo: "integracao"; quando: string | null }
  | { tipo: "pessoa"; quem: string; quando: string | null };

export function procedenciaDoCanal(canal: CanalOut): ProcedenciaDoCanal {
  if (canal.atualizado_por === null) {
    return { tipo: "herdada" };
  }
  if (ehAtorMaquina(canal.atualizado_por)) {
    return { tipo: "integracao", quando: canal.atualizado_em };
  }
  return { tipo: "pessoa", quem: canal.atualizado_por, quando: canal.atualizado_em };
}

/**
 * A frase de procedência, pronta para exibir.
 *
 * O verbo sai de `habilitado` (o estado atual) e não de um campo de "ação",
 * porque a API grava autor e estado na mesma transação: quem consta em
 * `atualizado_por` é o autor do estado que está lá agora.
 *
 * A data é omitida quando `atualizado_em` é nulo em vez de virar "data
 * desconhecida": os dois campos andam juntos hoje, e a guarda existe para o
 * tipo, não para inventar texto sobre um caso que não acontece.
 */
export function fraseDaProcedencia(canal: CanalOut): string {
  const procedencia = procedenciaDoCanal(canal);
  if (procedencia.tipo === "herdada") {
    return "Estado herdado da instalação: ninguém ligou nem desligou este canal até agora.";
  }

  const verbo = canal.habilitado ? "Ligado" : "Desligado";
  const quando = procedencia.quando === null ? "" : ` em ${formatarDataHora(procedencia.quando)}`;
  return procedencia.tipo === "integracao"
    ? `${verbo} pela chave de integração${quando}.`
    : `${verbo} por ${procedencia.quem}${quando}.`;
}

/** Quem de fato despacha: ligado **e** com credencial. */
export function canaisQueEnviam(canais: CanalOut[]): CanalOut[] {
  return canais.filter((canal) => canal.habilitado && canal.disponivel);
}

/**
 * Desligar este canal deixa a operação **sem nenhum canal enviando**?
 *
 * A conta é sobre `habilitado && disponivel`, e não sobre `habilitado` sozinho.
 * A razão é a consequência que o ADR 0006 pede para avisar — "a operação passa
 * a não ser avisada por nenhum caminho" —, e um canal ligado sem credencial não
 * é caminho nenhum: contá-lo como sobrevivente faria a confirmação enfraquecer
 * exatamente quando a operação está mais desprotegida.
 *
 * Devolve `true` também quando já não havia canal enviando antes. É correto: a
 * frase que isto governa descreve o estado **depois** da ação, e depois dela
 * continua não havendo nenhum.
 */
export function desligarSilenciaTudo(canais: CanalOut[], alvo: CanalAlerta): boolean {
  return canaisQueEnviam(canais).every((canal) => canal.canal === alvo);
}

type ParametrosDaUrl = Record<string, string | string[] | undefined>;

/** `?a=1&a=2` chega como array; o primeiro valor é o que vale. */
function primeiro(valor: string | string[] | undefined): string | undefined {
  return Array.isArray(valor) ? valor[0] : valor;
}

/**
 * A página do histórico pedida pela URL, descartando o que a API recusaria.
 *
 * A query string é entrada de fora: um `?offset=-1` colado de qualquer lugar
 * viraria 422 na listagem e derrubaria a tela inteira — inclusive o estado dos
 * canais, que é o dado principal e não tem nada a ver com o histórico.
 */
export function lerOffsetDoHistorico(params: ParametrosDaUrl): number {
  const offset = Number(primeiro(params.offset));
  return Number.isSafeInteger(offset) && offset > 0 ? offset : 0;
}

/**
 * O endereço da tela nesta página do histórico — o único lugar que monta esta
 * URL. Omite `offset=0` porque a primeira página é o default: sem isso, dois
 * endereços diferentes mostrariam a mesma tela.
 */
export function urlDoHistorico(offset: number): string {
  return offset > 0 ? `${CAMINHO_CANAIS}?offset=${offset}` : CAMINHO_CANAIS;
}
