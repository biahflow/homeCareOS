"use client";

import { X } from "lucide-react";
import { useState } from "react";
import { itensDoPapel } from "./nav-items";
import { SidebarContent } from "./SidebarContent";
import { Topbar } from "./Topbar";
import type { UsuarioDaShell } from "./usuario";

export function AppShell({
  children,
  usuario,
}: {
  children: React.ReactNode;
  usuario: UsuarioDaShell;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  // O filtro por papel acontece aqui, no ponto mais alto que já conhece o
  // `papel` — e não dentro da navegação, que passa a receber a lista pronta e
  // não precisa saber quem está logado. As duas `SidebarContent` (desktop e
  // gaveta) leem a mesma lista, senão um item restrito apareceria só numa das
  // larguras.
  const itens = itensDoPapel(usuario.papel);

  return (
    <div className="min-h-svh lg:grid lg:grid-cols-[254px_1fr]">
      <aside className="hidden border-r border-line bg-white px-3 lg:block">
        <SidebarContent itens={itens} />
      </aside>

      {menuOpen && (
        <div className="fixed inset-0 z-30 lg:hidden">
          <button
            type="button"
            aria-label="Fechar menu"
            className="absolute inset-0 bg-ink/40"
            onClick={() => setMenuOpen(false)}
          />
          <div className="relative flex h-full w-[254px] flex-col border-r border-line bg-white px-3">
            <button
              type="button"
              onClick={() => setMenuOpen(false)}
              aria-label="Fechar menu"
              className="icon-button mt-4 ml-auto"
            >
              <X size={18} />
            </button>
            <SidebarContent itens={itens} onNavigate={() => setMenuOpen(false)} />
          </div>
        </div>
      )}

      <div className="flex min-h-svh flex-col">
        <Topbar onOpenMenu={() => setMenuOpen(true)} usuario={usuario} />
        <main className="mx-auto w-full max-w-7xl flex-1 px-5 py-8 lg:px-9 lg:py-10">
          {children}
        </main>
      </div>
    </div>
  );
}
