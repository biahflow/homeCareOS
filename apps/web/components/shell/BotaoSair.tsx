"use client";

import { LogOut } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { logout } from "@homecareos/contracts";
import { API_BASE_URL } from "@/lib/env";

/**
 * Sair de verdade: revoga a sessão **no servidor** e só então sai da tela.
 *
 * A conferência acontece em estação compartilhada, em turnos, e a sessão dura
 * 12h no cookie. Um "sair" que apenas limpasse o estado do cliente deixaria a
 * sessão viva para o próximo que sentasse ali — ou para quem tivesse copiado o
 * cookie. Por isso a falha da chamada não é engolida: se a revogação não
 * aconteceu, dizer que a pessoa saiu seria mentira, e ela precisa poder tentar
 * de novo.
 */
export function BotaoSair() {
  const router = useRouter();
  const [saindo, setSaindo] = useState(false);
  const [erro, setErro] = useState(false);

  async function handleClick() {
    setErro(false);
    setSaindo(true);
    try {
      await logout(API_BASE_URL);
    } catch {
      setSaindo(false);
      setErro(true);
      return;
    }

    router.replace("/login");
    // Sem isto, o payload já renderizado da área logada continuaria no cache de
    // rotas do cliente: quem entrasse em seguida na mesma estação poderia ver,
    // por um instante, o nome e o papel de quem acabou de sair.
    router.refresh();
  }

  return (
    <div className="flex items-center gap-2">
      {erro && (
        <span role="alert" className="text-[11px] font-semibold text-danger">
          Não foi possível sair. Tente de novo.
        </span>
      )}
      <button
        type="button"
        onClick={handleClick}
        disabled={saindo}
        className="btn btn--ghost px-3"
      >
        <LogOut size={16} />
        {saindo ? "Saindo…" : "Sair"}
      </button>
    </div>
  );
}
