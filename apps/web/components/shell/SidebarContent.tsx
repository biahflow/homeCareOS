import Link from "next/link";
import { NavList } from "./NavList";

/**
 * Conteúdo da sidebar: marca, navegação e rodapé. Usado tanto na sidebar fixa
 * de desktop quanto na gaveta mobile — mesma marcação, dois contêineres.
 */
export function SidebarContent({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <div className="flex h-full flex-col gap-6 py-5">
      <div className="brand-row">
        <span className="brand-mark">HC</span>
        <div className="grid">
          <span className="text-sm font-semibold tracking-[-0.01em] text-ink">
            Home<span className="text-brand-500">CareOS</span>
          </span>
          <span className="text-[11px] text-muted">Conferência de faturamento</span>
        </div>
      </div>

      <NavList onNavigate={onNavigate} />

      <div className="sidebar-bottom">
        <p className="text-[11px] leading-5 text-muted">
          Sem autenticação real nesta versão (issue #6).
        </p>
        <Link href="/login" className="nav-item px-0 text-brand-600 hover:bg-transparent">
          Voltar ao login
        </Link>
      </div>
    </div>
  );
}
