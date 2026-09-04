import { AuthShell } from "@/components/auth/AuthShell";
import { FormularioEsqueciSenha } from "@/components/auth/FormularioEsqueciSenha";

/**
 * Pedido de link de redefinição de senha. **Fora** do grupo `(autenticado)`:
 * quem esqueceu a senha não tem sessão para apresentar, exatamente como
 * `/login` — e `POST /api/auth/senha/esqueci` está registrado sem dependency
 * de autorização pelo mesmo motivo (`auth/router.py:esqueci_senha`).
 *
 * Sem `searchParams` nesta página: ao contrário de `/redefinir-senha`, não há
 * nenhum dado sensível chegando pela URL aqui.
 */
export default function EsqueciSenhaPage() {
  return (
    <AuthShell>
      <FormularioEsqueciSenha />
    </AuthShell>
  );
}
