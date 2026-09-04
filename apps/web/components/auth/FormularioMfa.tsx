"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useId, useState } from "react";
import { ApiError, verificarMfa } from "@homecareos/contracts";
import { API_BASE_URL } from "@/lib/env";

const MENSAGEM_BLOQUEIO =
  "Muitas tentativas de verificação. Aguarde alguns minutos antes de tentar novamente.";

/**
 * Mensagem do 401, e ela nomeia as duas causas porque a API não as distingue.
 *
 * `POST /api/auth/mfa/verificar` responde o mesmo 401 para código errado e para
 * sessão pendente que não existe mais — expirada, revogada ou substituída por
 * um login novo (`auth/router.py`, `_credencial_invalida` nos dois caminhos).
 * Distinguir na resposta diria a quem está com um cookie velho por que ele não
 * vale. Como o cliente também não tem como saber, ele não escolhe uma das duas
 * histórias para contar: apresenta as duas e deixa o caminho de volta ao login
 * à mão.
 */
const MENSAGEM_CODIGO_RECUSADO =
  "Código não aceito. Ele pode estar incorreto, ou a sessão do login pode ter expirado.";

const MENSAGEM_CAMPO = "Informe o código.";
const MENSAGEM_INESPERADA = "Falha inesperada ao verificar o código. Tente novamente.";

function mensagemDeErro(erro: unknown): string {
  if (!(erro instanceof ApiError)) {
    return MENSAGEM_INESPERADA;
  }
  if (erro.status === 429) {
    return MENSAGEM_BLOQUEIO;
  }
  if (erro.status === 401) {
    return MENSAGEM_CODIGO_RECUSADO;
  }
  return erro.message;
}

export function FormularioMfa() {
  const router = useRouter();
  const codigoId = useId();
  const ajudaId = useId();
  const [enviando, setEnviando] = useState(false);
  const [erro, setErro] = useState<string | null>(null);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const dados = new FormData(event.currentTarget);
    const codigo = String(dados.get("codigo") ?? "").trim();

    if (codigo === "") {
      setErro(MENSAGEM_CAMPO);
      return;
    }

    setErro(null);
    setEnviando(true);
    try {
      // A sessão pendente vem do cookie, não de estado desta tela: recarregar a
      // página aqui é caminho suportado, e a autoridade continua sendo a
      // resposta da API.
      await verificarMfa(API_BASE_URL, { codigo });
      router.replace("/documentos");
      router.refresh();
    } catch (causa) {
      setEnviando(false);
      setErro(mensagemDeErro(causa));
    }
  }

  return (
    <>
      <div className="page-head">
        <p className="eyebrow">Verificação em duas etapas</p>
        <h1>Confirme que é você</h1>
        <p>A senha foi aceita. Falta o segundo fator para abrir a sessão.</p>
      </div>

      <form className="form-grid" onSubmit={handleSubmit} noValidate>
        <div className="grid gap-1.5">
          <label htmlFor={codigoId} className="form-label">
            Código de verificação
          </label>
          <input
            id={codigoId}
            name="codigo"
            type="text"
            // Um campo só para os dois formatos, como a API exige: dois campos
            // separados diriam a quem sonda qual dos caminhos falhou.
            autoComplete="one-time-code"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            placeholder="000000"
            aria-describedby={ajudaId}
            className="field"
            disabled={enviando}
            autoFocus
          />
          <p id={ajudaId} className="text-xs text-muted">
            Os seis dígitos do app autenticador ou um dos códigos de recuperação
            (<code>a1b2c-3d4e5</code>).
          </p>
        </div>

        <button type="submit" className="btn btn--primary mt-2" disabled={enviando}>
          {enviando ? "Verificando…" : "Verificar"}
        </button>

        {erro && (
          <p role="alert" className="alert--error">
            {erro}
          </p>
        )}

        <Link href="/login" className="btn btn--ghost">
          Entrar novamente
        </Link>
      </form>
    </>
  );
}
