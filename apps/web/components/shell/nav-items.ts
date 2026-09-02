import { FileStack, ListChecks, BarChart3 } from "lucide-react";
import type { ComponentType } from "react";

export interface NavItem {
  href: string;
  label: string;
  icon: ComponentType<{ size?: number; className?: string }>;
}

export const NAV_ITEMS: NavItem[] = [
  { href: "/documentos", label: "Documentos", icon: FileStack },
  { href: "/pendencias", label: "Pendências", icon: ListChecks },
  { href: "/relatorios", label: "Relatórios", icon: BarChart3 },
];
