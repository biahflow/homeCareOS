import { redirect } from "next/navigation";
import { AppShell } from "@/components/shell/AppShell";
import { usuarioDaSessao } from "@/lib/sessao";

/**
 * Server Component: nada da área logada é renderizado antes de a API dizer
 * quem é a pessoa.
 *
 * A verificação vive aqui, e não no `proxy.ts`, porque a doc do Next é
 * explícita ao dizer que o Proxy não deve ser a solução de sessão ou
 * autorização (`node_modules/next/dist/docs/01-app/01-getting-started/16-proxy.md`)
 * — ele roda em toda rota, inclusive nas prefetchadas, e serve para checagem
 * otimista. A pergunta cara ("esta sessão vale?") é feita uma vez, aqui, contra
 * a API.
 *
 * Isto **não** é a última linha de defesa, e não deve ser confundida com uma:
 * quem protege prontuário é a API, que autentica cada chamada a `/api/*` por si
 * — inclusive as que partem desta shell. Por Partial Rendering, um layout não
 * re-renderiza a cada navegação dentro do próprio grupo; se o servidor
 * derrubar a sessão no meio do turno, quem devolve 401 é a chamada seguinte à
 * API, e é ela que manda a pessoa de volta para o login.
 */
export default async function AutenticadoLayout({ children }: { children: React.ReactNode }) {
  const usuario = await usuarioDaSessao();
  if (usuario === null) {
    // Sem sessão válida — e "sem sessão" inclui a que parou no primeiro fator.
    redirect("/login");
  }

  return (
    <AppShell usuario={{ nome: usuario.nome, papel: usuario.papel }}>{children}</AppShell>
  );
}
