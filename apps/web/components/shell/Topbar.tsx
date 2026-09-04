"use client";

import { Menu } from "lucide-react";
import { usePathname } from "next/navigation";
import { BotaoSair } from "./BotaoSair";
import { NAV_ITEMS } from "./nav-items";
import { PAPEL_LABEL } from "./usuario";
import type { UsuarioDaShell } from "./usuario";

export function Topbar({
  onOpenMenu,
  usuario,
}: {
  onOpenMenu: () => void;
  usuario: UsuarioDaShell;
}) {
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

      <div className="ml-auto flex items-center gap-3">
        {/* Nome e papel de quem está logado, vindos de `GET /api/auth/eu`. Só
            aparecem porque a sessão já foi verificada no servidor: até lá não há
            usuário nenhum para esta interface mostrar. */}
        <div className="hidden text-right leading-tight sm:grid">
          <span className="text-[13px] font-semibold text-ink">{usuario.nome}</span>
          <span className="text-[11px] text-muted">{PAPEL_LABEL[usuario.papel]}</span>
        </div>
        <BotaoSair />
      </div>
    </header>
  );
}
