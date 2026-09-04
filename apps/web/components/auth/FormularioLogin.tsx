"use client";

import { useRouter } from "next/navigation";
import { useId, useState } from "react";
import { ApiError, ehUsuario, login } from "@homecareos/contracts";
import { API_BASE_URL } from "@/lib/env";

/**
 * Mensagem do 429. É a única que escrevemos por conta própria neste formulário,
 * e existe porque a API tem um freio contra força bruta (atraso progressivo e
 * trava por IP e por conta) que hoje não teria tela: sem isto, quem foi
 * bloqueado veria "credencial inválida" e concluiria que a própria senha
 * mudou. O texto não diz quanto falta nem se a trava é de conta ou de origem —
 * a API mantém as duas indistinguíveis de propósito.
 */
const MENSAGEM_BLOQUEIO =
  "Muitas tentativas de acesso. Aguarde alguns minutos antes de tentar novamente.";

const MENSAGEM_CAMPOS = "Informe o e-mail e a senha.";
const MENSAGEM_INESPERADA = "Falha inesperada ao entrar. Tente novamente.";

function mensagemDeErro(erro: unknown): string {
  if (!(erro instanceof ApiError)) {
    return MENSAGEM_INESPERADA;
  }
  if (erro.status === 429) {
    return MENSAGEM_BLOQUEIO;
  }
  // 401 sai exatamente como a API escreveu. Ela responde o mesmo para e-mail
  // inexistente, senha errada e usuário inativo — deduzir qual dos três foi, ou
  // "melhorar" o texto com uma dica, entregaria a quem sonda o que a API se
  // recusa a dizer.
  return erro.message;
}

export function FormularioLogin({ aviso }: { aviso?: string }) {
  const router = useRouter();
  const emailId = useId();
  const senhaId = useId();
  const [enviando, setEnviando] = useState(false);
  const [erro, setErro] = useState<string | null>(null);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // Lidos do formulário, não guardados em estado do React: a senha vive só o
    // tempo desta função e do DOM que o navegador já controla.
    const dados = new FormData(event.currentTarget);
    const email = String(dados.get("email") ?? "").trim();
    const senha = String(dados.get("senha") ?? "");

    if (email === "" || senha === "") {
      setErro(MENSAGEM_CAMPOS);
      return;
    }

    setErro(null);
    setEnviando(true);
    try {
      const resposta = await login(API_BASE_URL, { email, senha });

      // A resposta manda, e não a ausência de erro: com MFA ativado o login
      // devolve 200 sem dado nenhum do usuário, e a área logada não pode ser
      // desenhada antes do segundo fator.
      router.replace(ehUsuario(resposta) ? "/documentos" : "/mfa");

      // Limpa o cache de rotas do cliente antes de entrar. Numa estação
      // compartilhada, o payload da área logada de quem usou o turno anterior
      // pode ter sobrado em memória — e ele traz nome e papel de outra pessoa.
      router.refresh();
      // `enviando` continua ligado de propósito: a navegação é assíncrona, e
      // reabilitar o botão aqui permitiria um segundo login no meio dela.
    } catch (causa) {
      setEnviando(false);
      setErro(mensagemDeErro(causa));
    }
  }

  return (
    <>
      <div className="page-head">
        <p className="eyebrow">Acesso</p>
        <h1>Entrar</h1>
        <p>Faturamento e conferência de evoluções.</p>
      </div>

      <form className="form-grid" onSubmit={handleSubmit} noValidate>
        {aviso && (
          <p role="status" className="alert--info">
            {aviso}
          </p>
        )}

        <div className="grid gap-1.5">
          <label htmlFor={emailId} className="form-label">
            E-mail
          </label>
          <input
            id={emailId}
            name="email"
            type="email"
            autoComplete="email"
            placeholder="voce@empresa.com"
            className="field"
            disabled={enviando}
          />
        </div>

        <div className="grid gap-1.5">
          <label htmlFor={senhaId} className="form-label">
            Senha
          </label>
          <input
            id={senhaId}
            name="senha"
            type="password"
            autoComplete="current-password"
            placeholder="••••••••"
            className="field"
            disabled={enviando}
          />
        </div>

        <button type="submit" className="btn btn--primary mt-2" disabled={enviando}>
          {enviando ? "Entrando…" : "Entrar"}
        </button>

        {erro && (
          <p role="alert" className="alert--error">
            {erro}
          </p>
        )}
      </form>
    </>
  );
}
