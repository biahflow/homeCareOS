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
        // `startsWith` com a barra, e não igualdade: numa sub-rota como
        // `/documentos/{id}` o item precisa continuar marcado, senão a
        // navegação perde a pessoa justamente quando ela desceu um nível. A
        // barra evita que `/documentos` case com um futuro `/documentosX`.
        const isActive = pathname === item.href || pathname.startsWith(`${item.href}/`);
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
