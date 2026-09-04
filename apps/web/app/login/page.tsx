import { AuthShell } from "@/components/auth/AuthShell";
import { FormularioLogin } from "@/components/auth/FormularioLogin";

/**
 * Avisos que a própria aplicação pede pela URL ao mandar alguém de volta para
 * cá — hoje só o da sessão encerrada por outro login no mesmo navegador.
 *
 * O valor que vem na query **nunca** é renderizado: ele só serve de chave neste
 * mapa. Ecoar o parâmetro cru deixaria qualquer link montado por terceiro
 * escrever o que quisesse na tela de login, que é o lugar onde a pessoa decide
 * se confia no que está lendo.
 */
const AVISOS: Record<string, string> = {
  "sessao-encerrada": "Sua sessão foi encerrada. Entre novamente para continuar.",
};

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ motivo?: string | string[] }>;
}) {
  const { motivo } = await searchParams;
  const aviso = typeof motivo === "string" ? AVISOS[motivo] : undefined;

  return (
    <AuthShell>
      <FormularioLogin aviso={aviso} />
    </AuthShell>
  );
}
