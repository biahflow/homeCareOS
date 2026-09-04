import type { PendenciaStatus } from "@homecareos/contracts";

/**
 * Como o prazo de uma pendência é lido e apresentado.
 *
 * Módulo separado do componente por uma razão de regra, não de organização:
 * ler o relógio é impuro, e `react-hooks/purity` recusa `Date.now()` dentro de
 * um componente — com razão, porque num Client Component um valor que muda
 * entre dois renders quebra a hidratação. Aqui a leitura é legítima e
 * necessária ("este prazo já passou?"), acontece **uma vez por requisição** no
 * servidor e viaja para o navegador já decidida. Isolá-la aqui mantém a regra
 * valendo onde ela protege, sem obrigar a tela a mentir sobre o prazo.
 */

/**
 * Fuso fixo, e não o do servidor: o mesmo container roda em UTC em produção e
 * no fuso da máquina em desenvolvimento, e um prazo que muda de dia conforme
 * onde o processo está hospedado é pior que um prazo em fuso errado — ele
 * discorda de si mesmo entre ambientes. A operação é brasileira; o horário é o
 * dela.
 */
const FORMATO_DE_DATA = new Intl.DateTimeFormat("pt-BR", {
  dateStyle: "short",
  timeStyle: "short",
  timeZone: "America/Sao_Paulo",
});

export function formatarPrazo(iso: string): string {
  return FORMATO_DE_DATA.format(new Date(iso));
}

/**
 * Congela o instante da renderização e devolve o teste de "prazo vencido".
 *
 * Um instante só para a lista inteira: perguntar as horas a cada linha faria
 * duas pendências com o mesmo prazo serem classificadas diferente quando o
 * render atravessa a virada do segundo.
 *
 * `resolvida` nunca conta como vencida — o prazo dela já não cobra nada de
 * ninguém, e destacá-la em vermelho mandaria a equipe atrás de trabalho que já
 * foi feito.
 */
export function marcadorDeVencimento(): (
  deadline: string,
  status: PendenciaStatus,
) => boolean {
  const agora = Date.now();
  return (deadline, status) => status !== "resolvida" && new Date(deadline).getTime() < agora;
}
