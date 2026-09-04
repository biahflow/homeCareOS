import { NavList } from "./NavList";

/**
 * Conteúdo da sidebar: marca e navegação. Usado tanto na sidebar fixa de
 * desktop quanto na gaveta mobile — mesma marcação, dois contêineres.
 *
 * O rodapé que existia aqui ("sem autenticação real nesta versão") saiu porque
 * deixou de ser verdade, e um aviso falso na tela é pior que nenhum. Sair é
 * ação de sessão e mora no `Topbar`, visível em qualquer largura — atrás da
 * gaveta, quem quisesse encerrar o turno teria de abrir um menu antes.
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
    </div>
  );
}
