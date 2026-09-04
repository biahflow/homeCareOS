"use client";

import Link from "next/link";
import { useId, useState } from "react";
import { esqueciSenha } from "@homecareos/contracts";
import { API_BASE_URL } from "@/lib/env";

/**
 * A única mensagem de confirmação, para qualquer e-mail digitado.
 *
 * `POST /api/auth/senha/esqueci` responde **204 sempre** — e-mail cadastrado
 * ou não, ativo ou não, teto de envios atingido ou não, SMTP configurado ou
 * não (`auth/router.py:esqueci_senha`). Não existe leitura da resposta que
 * diga qual foi o caso, e por isso este texto não afirma que um e-mail *foi*
 * enviado — só que ele será, *se* a conta existir. Mostrar "e-mail não
 * encontrado" para o outro caso reabriria a enumeração de contas que a issue
 * #30 fechou no login.
 */
const MENSAGEM_CONFIRMACAO =
  "Se este e-mail estiver cadastrado, enviamos um link para redefinir a senha. O link vale por tempo limitado e só pode ser usado uma vez.";

const MENSAGEM_CAMPO = "Informe o e-mail.";
// Único erro que este formulário pode mostrar de fato: o endpoint não tem
// nenhum outro caminho de falha para um corpo válido (ver a docstring de
// `esqueciSenha`). Cai aqui apenas quando a chamada nem chega a sair —
// problema de rede/API fora do ar — o que não distingue e-mail nenhum: é o
// mesmo texto para qualquer entrada, existente ou não.
const MENSAGEM_INESPERADA = "Falha ao enviar o pedido. Verifique sua conexão e tente novamente.";

export function FormularioEsqueciSenha() {
  const emailId = useId();
  const [enviando, setEnviando] = useState(false);
  const [enviado, setEnviado] = useState(false);
  const [erro, setErro] = useState<string | null>(null);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const dados = new FormData(event.currentTarget);
    const email = String(dados.get("email") ?? "").trim();

    if (email === "") {
      setErro(MENSAGEM_CAMPO);
      return;
    }

    setErro(null);
    setEnviando(true);
    try {
      await esqueciSenha(API_BASE_URL, { email });
      // A resposta não diz se a conta existe — só que a chamada terminou sem
      // erro de transporte. `enviado` dispara a mesma tela de confirmação
      // sempre, e é por isso que não há ramo diferente para tratar aqui.
      setEnviado(true);
    } catch {
      setEnviando(false);
      setErro(MENSAGEM_INESPERADA);
    }
  }

  if (enviado) {
    return (
      <>
        <div className="page-head">
          <p className="eyebrow">Recuperação de senha</p>
          <h1>Verifique seu e-mail</h1>
        </div>
        <p role="status" className="alert--info">
          {MENSAGEM_CONFIRMACAO}
        </p>
        <Link href="/login" className="btn btn--ghost mt-2">
          Voltar para o login
        </Link>
      </>
    );
  }

  return (
    <>
      <div className="page-head">
        <p className="eyebrow">Recuperação de senha</p>
        <h1>Esqueceu sua senha?</h1>
        <p>Informe o e-mail da sua conta e enviaremos um link para redefinir a senha.</p>
      </div>

      <form className="form-grid" onSubmit={handleSubmit} noValidate>
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

        <button type="submit" className="btn btn--primary mt-2" disabled={enviando}>
          {enviando ? "Enviando…" : "Enviar link"}
        </button>

        {erro && (
          <p role="alert" className="alert--error">
            {erro}
          </p>
        )}

        <Link href="/login" className="btn btn--ghost">
          Voltar para o login
        </Link>
      </form>
    </>
  );
}
