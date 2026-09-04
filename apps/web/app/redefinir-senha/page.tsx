import { AuthShell } from "@/components/auth/AuthShell";
import { FormularioRedefinirSenha } from "@/components/auth/FormularioRedefinirSenha";

/**
 * Redefinição de senha a partir do link do e-mail. **Fora** do grupo
 * `(autenticado)`, pelo mesmo motivo de `/esqueci-senha`: quem chega aqui não
 * tem sessão, e `POST /api/auth/senha/redefinir` também não exige uma.
 *
 * `token` chega por `searchParams` (Next 16: é `Promise`, não prop síncrona —
 * ver `node_modules/next/dist/docs/01-app/03-api-reference/03-file-conventions/page.md`).
 * Esta página só lê o valor e repassa; a limpeza da URL
 * (`history.replaceState`) precisa acontecer no navegador, e por isso vive no
 * componente cliente (`FormularioRedefinirSenha`), que também é onde o token
 * passa a existir só em memória. String vazia conta como "sem token": um link
 * mal copiado não deve abrir o formulário para depois falhar num 422 sem
 * explicação melhor que a do estado "sem token".
 */
export default async function RedefinirSenhaPage({
  searchParams,
}: {
  searchParams: Promise<{ token?: string | string[] }>;
}) {
  const { token } = await searchParams;
  const tokenInicial = typeof token === "string" && token !== "" ? token : undefined;

  return (
    <AuthShell>
      <FormularioRedefinirSenha tokenInicial={tokenInicial} />
    </AuthShell>
  );
}
