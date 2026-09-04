"use client";

import { Menu } from "lucide-react";
import { usePathname } from "next/navigation";
import { NAV_ITEMS } from "./nav-items";

export function Topbar({ onOpenMenu }: { onOpenMenu: () => void }) {
  const pathname = usePathname();
  const current = NAV_ITEMS.find((item) => item.href === pathname);

  return (
    <header className="sticky top-0 z-20 flex h-[68px] items-center gap-3 border-b border-line bg-canvas/85 px-5 backdrop-blur-xl lg:px-9">
      <button
        type="button"
        onClick={onOpenMenu}
        aria-label="Abrir menu"
        className="icon-button lg:hidden"
      >
        <Menu size={18} />
      </button>

      <p className="breadcrumb">
        Operação {current ? <> / <strong>{current.label}</strong></> : null}
      </p>
    </header>
  );
}
