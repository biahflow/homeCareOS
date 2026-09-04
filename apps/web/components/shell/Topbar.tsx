"use client";

import { Menu, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { BotaoSair } from "./BotaoSair";
import { NAV_ITEMS } from "./nav-items";
import { PAPEL_LABEL } from "./usuario";
import type { UsuarioDaShell } from "./usuario";

/**
 * As rotas de conta, que **não** entram em `NAV_ITEMS` de propósito: a
 * navegação lateral é de operação, e conta não é operação. Elas ainda precisam
 * de rótulo aqui, senão o breadcrumb anuncia "Operação" numa tela que não é.
 */
const PAGINAS_DE_CONTA: Record<string, string> = {
  "/conta/seguranca": "Segurança",
};

export function Topbar({
  onOpenMenu,
  usuario,
}: {
  onOpenMenu: () => void;
  usuario: UsuarioDaShell;
}) {
  const pathname = usePathname();
  const current = NAV_ITEMS.find((item) => item.href === pathname);
  const paginaDeConta = PAGINAS_DE_CONTA[pathname];

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
        {paginaDeConta ? (
          <>
            Conta / <strong>{paginaDeConta}</strong>
          </>
        ) : (
          <>Operação {current ? <> / <strong>{current.label}</strong></> : null}</>
        )}
      </p>

      <div className="ml-auto flex items-center gap-3">
        {/* Nome e papel de quem está logado, vindos de `GET /api/auth/eu`. Só
            aparecem porque a sessão já foi verificada no servidor: até lá não há
            usuário nenhum para esta interface mostrar. */}
        <div className="hidden text-right leading-tight sm:grid">
          <span className="text-[13px] font-semibold text-ink">{usuario.nome}</span>
          <span className="text-[11px] text-muted">{PAPEL_LABEL[usuario.papel]}</span>
        </div>
        {/* Segurança da conta fica aqui, junto do nome e do Sair, e não na
            navegação lateral: é o canto da pessoa, não do trabalho dela. O
            rótulo some nas telas estreitas, onde o espaço é do breadcrumb — daí
            o `aria-label`, que é o nome acessível quando o texto não aparece. */}
        <Link
          href="/conta/seguranca"
          aria-label="Segurança da conta"
          aria-current={pathname === "/conta/seguranca" ? "page" : undefined}
          className="btn btn--ghost px-3"
        >
          <ShieldCheck size={16} />
          <span className="hidden sm:inline">Segurança</span>
        </Link>
        <BotaoSair />
      </div>
    </header>
  );
}
