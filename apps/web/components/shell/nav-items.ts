import { FileStack, ListChecks, BarChart3, Users, BellRing } from "lucide-react";
import type { ComponentType } from "react";
import type { Papel } from "@homecareos/contracts";

export interface NavItem {
  href: string;
  label: string;
  icon: ComponentType<{ size?: number; className?: string }>;
  /**
   * Quem vê o item. **Ausente = todos os papéis**, que é o caso das três telas
   * de operação: os três papéis as leem, e é a ação dentro delas que restringe.
   */
  papeis?: readonly Papel[];
}

export const NAV_ITEMS: NavItem[] = [
  { href: "/documentos", label: "Documentos", icon: FileStack },
  { href: "/pendencias", label: "Pendências", icon: ListChecks },
  { href: "/relatorios", label: "Relatórios", icon: BarChart3 },
  // `/usuarios` é o primeiro item com público restrito, e por um motivo que as
  // outras telas não têm: `/api/usuarios` inteira exige coordenador (ADR 0004),
  // então conferente e gestor não têm nem o que ler ali. Nas outras três o papel
  // decide o que se pode *fazer*, não se a tela abre.
  { href: "/usuarios", label: "Usuários", icon: Users, papeis: ["coordenador"] },
  // `/api/alertas` inteira exige coordenador ou gestor (issue #30) — conferente
  // não tem nem o que ler ali, mesmo motivo de `/usuarios` acima.
  { href: "/alertas", label: "Alertas", icon: BellRing, papeis: ["coordenador", "gestor"] },
];

/**
 * Os itens que este papel vê.
 *
 * Sumir com o item é **conveniência, não proteção**: quem digitar `/usuarios` na
 * barra de endereços não passa por aqui. Quem recusa é a página (que checa o
 * papel antes de qualquer chamada) e, por último, a API. Esta função existe para
 * a navegação não oferecer uma porta que vai bater na cara de quem a abrir.
 */
export function itensDoPapel(papel: Papel): NavItem[] {
  return NAV_ITEMS.filter((item) => item.papeis === undefined || item.papeis.includes(papel));
}
