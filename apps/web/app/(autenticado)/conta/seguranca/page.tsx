import { PainelSegundoFator } from "@/components/conta/PainelSegundoFator";

/**
 * Segurança da conta: cadastro e desativação do segundo fator.
 *
 * Dentro do grupo `(autenticado)` porque configurar MFA exige sessão
 * **completa** — o layout do grupo já derruba para `/login` quem não a tem, e a
 * API recusa estes endpoints para chave de máquina (403) e para sessão pendente
 * (401).
 *
 * A página é só a casca, e o miolo é Client Component por uma razão que não é
 * preferência: `POST /api/auth/mfa/iniciar` **substitui** o segredo a cada
 * chamada. Buscá-lo aqui teria dois defeitos de uma vez — um `POST` com efeito
 * colateral no render (que o Next pode repetir), e o segredo viajando no
 * payload RSC do HTML e ficando no cache de rotas do cliente. A credencial é
 * pedida pelo navegador, por clique explícito, e vive em memória.
 */
export default function SegurancaPage() {
  return (
    <div className="grid gap-6">
      <div className="page-head">
        <p className="eyebrow">Conta</p>
        <h1>Segurança</h1>
        <p>
          Verificação em duas etapas da sua conta: cadastro no aplicativo autenticador, códigos de
          recuperação e desativação.
        </p>
      </div>

      <PainelSegundoFator />
    </div>
  );
}
