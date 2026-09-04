"use client";

import { ArrowLeft, ShieldOff } from "lucide-react";
import { useRouter } from "next/navigation";
import { useId, useState } from "react";
import { ApiError, desativarMfa } from "@homecareos/contracts";
import { API_BASE_URL } from "@/lib/env";

/**
 * Mensagem do 422 — **uma só**, cobrindo senha errada e código errado.
 *
 * A API responde o mesmo para os dois de propósito (`MfaDesativarRequest`:
 * "duas mensagens diriam qual dos dois o atacante já tem"). Separar aqui, ou
 * deduzir qual falhou, desfaria essa proteção justamente na tela em que ela
 * importa: quem chegou até aqui com a sessão de outra pessoa está tentando
 * descobrir o que ainda lhe falta.
 */
const MENSAGEM_RECUSADO =
  "Senha ou código não conferem. A API não informa qual dos dois falhou — confira os dois e tente de novo.";

const MENSAGEM_CAMPOS = "Informe a senha e o código do aplicativo autenticador.";
const MENSAGEM_INESPERADA = "Falha inesperada ao desativar o segundo fator. Tente novamente.";

function mensagemDeErro(erro: unknown): string {
  if (!(erro instanceof ApiError)) {
    return MENSAGEM_INESPERADA;
  }
  if (erro.status === 422) {
    return MENSAGEM_RECUSADO;
  }
  return erro.message;
}

export type ResultadoDesativacao = "desativado" | "nao-ativado";

/**
 * Desligar o segundo fator: senha **e** código atual, com confirmação explícita.
 *
 * A API exige os dois fatores, e não é excesso de zelo: com só o código, uma
 * sessão sequestrada desligaria sozinha a proteção que existe contra ela; com
 * só a senha, bastaria a senha vazada — que é a hipótese que faz alguém ativar
 * MFA. O formulário não tenta compensar nem suavizar isso.
 *
 * A confirmação marcada à mão existe porque a operação é destrutiva e
 * silenciosa: ela apaga o segredo **e os códigos de recuperação**, responde 204
 * sem dizer o que levou junto, e não tem desfazer. O que a pessoa precisa saber
 * antes de submeter está do lado do botão que submete, não numa tela anterior
 * que ela já esqueceu.
 */
export function FormularioDesativarMfa({
  onConcluido,
  onCancelar,
}: {
  onConcluido: (resultado: ResultadoDesativacao) => void;
  onCancelar: () => void;
}) {
  const router = useRouter();
  const senhaId = useId();
  const codigoId = useId();
  const cienteId = useId();
  const [ciente, setCiente] = useState(false);
  const [enviando, setEnviando] = useState(false);
  const [erro, setErro] = useState<string | null>(null);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // Lidos do formulário, não guardados em estado do React: a senha e o código
    // vivem o tempo desta função e do DOM que o navegador já controla.
    const dados = new FormData(event.currentTarget);
    const senha = String(dados.get("senha") ?? "");
    const codigo = String(dados.get("codigo") ?? "").trim();

    if (senha === "" || codigo === "") {
      setErro(MENSAGEM_CAMPOS);
      return;
    }

    setErro(null);
    setEnviando(true);
    try {
      await desativarMfa(API_BASE_URL, { senha, codigo });
      onConcluido("desativado");
      // `enviando` continua ligado de propósito: quem chama já trocou de passo.
    } catch (causa) {
      if (causa instanceof ApiError && causa.status === 401) {
        // A sessão acabou no servidor no meio da operação — mesmo tratamento do
        // resto da área logada: voltar ao login dizendo o que houve.
        router.replace("/login?motivo=sessao-encerrada");
        router.refresh();
        return;
      }
      setEnviando(false);
      if (causa instanceof ApiError && causa.status === 409) {
        // O segundo fator não estava ativado. Como `GET /api/auth/eu` não expõe
        // esse campo, este 409 é informação nova — e não erro de quem digitou.
        onConcluido("nao-ativado");
        return;
      }
      setErro(mensagemDeErro(causa));
    }
  }

  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>Desativar o segundo fator</h2>
        <span className="state state--3">Ação destrutiva</span>
      </div>

      <div className="grid gap-4">
        <p className="text-sm leading-6 text-muted">
          Desligar o segundo fator faz o login voltar a exigir apenas a senha. Os seus{" "}
          <strong className="text-ink">códigos de recuperação atuais deixam de valer</strong> e não
          podem ser recuperados: religar o segundo fator gera um segredo novo e uma lista nova.
        </p>

        <form className="form-grid" onSubmit={handleSubmit} noValidate>
          <div className="grid max-w-xs gap-1.5">
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

          <div className="grid max-w-xs gap-1.5">
            <label htmlFor={codigoId} className="form-label">
              Código do aplicativo autenticador
            </label>
            <input
              id={codigoId}
              name="codigo"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              placeholder="000000"
              className="field"
              disabled={enviando}
            />
          </div>

          <label htmlFor={cienteId} className="flex items-start gap-2.5 text-sm text-ink">
            <input
              id={cienteId}
              type="checkbox"
              checked={ciente}
              onChange={(event) => setCiente(event.target.checked)}
              disabled={enviando}
              className="mt-0.5 size-4 accent-brand-500"
            />
            Entendo que os códigos de recuperação atuais deixam de valer.
          </label>

          {erro && (
            <p role="alert" className="alert--error">
              {erro}
            </p>
          )}

          <div className="flex flex-wrap gap-2">
            <button
              type="submit"
              disabled={enviando || !ciente}
              // Sem `.btn--primary`: a cor de ação principal desta interface
              // convida ao clique, e este botão não deve convidar. `--color-danger`
              // já é o vermelho do tema (ver `globals.css`).
              className="btn bg-danger text-white hover:bg-red-800"
            >
              <ShieldOff size={16} />
              {enviando ? "Desativando…" : "Desativar segundo fator"}
            </button>
            <button
              type="button"
              onClick={onCancelar}
              disabled={enviando}
              className="btn btn--ghost"
            >
              <ArrowLeft size={16} />
              Cancelar
            </button>
          </div>
        </form>
      </div>
    </section>
  );
}
