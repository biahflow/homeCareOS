import { AuthShell } from "@/components/auth/AuthShell";
import { FormularioMfa } from "@/components/auth/FormularioMfa";

/**
 * Segundo fator do login. **Fora** do grupo `(autenticado)`: quem chega aqui
 * apresentou a senha e nada mais — a sessão que ele tem é pendente, e ela não
 * abre rota nenhuma de `/api/*`.
 *
 * A página não verifica sessão antes de renderizar, e é decisão consciente:
 * `GET /api/auth/eu` devolve 401 para sessão pendente exatamente como para quem
 * não tem sessão (`auth/sessoes.py:97`), então não há pergunta a fazer que
 * distinga os dois. Renderizar sempre é o que faz recarregar esta tela ser um
 * caminho suportado; quem submeter sem sessão pendente viva recebe 401 da API e
 * vê o erro aqui, com o caminho de volta ao login à mão.
 */
export default function MfaPage() {
  return (
    <AuthShell>
      <FormularioMfa />
    </AuthShell>
  );
}
