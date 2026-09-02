"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { NAV_ITEMS } from "./nav-items";

/**
 * Lista de navegação da shell autenticada. Uma única função devolve o `<nav>`
 * tanto para a sidebar de desktop quanto para a gaveta mobile — nunca duas
 * cópias da marcação, senão um item novo aparece só numa das larguras.
 */
export function NavList({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();

  return (
    <nav aria-label="Navegação principal" className="grid gap-1">
      <p className="nav-label">Operação</p>
      {NAV_ITEMS.map((item) => {
        const isActive = pathname === item.href;
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            onClick={onNavigate}
            aria-current={isActive ? "page" : undefined}
            className={`nav-item${isActive ? " nav-item--active" : ""}`}
          >
            <Icon size={16} />
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
