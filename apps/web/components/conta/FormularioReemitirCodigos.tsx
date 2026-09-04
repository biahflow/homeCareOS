"use client";

import { ArrowLeft, KeyRound } from "lucide-react";
import { useRouter } from "next/navigation";
import { useId, useState } from "react";
import { ApiError, reemitirCodigosMfa } from "@homecareos/contracts";
import { API_BASE_URL } from "@/lib/env";

/**
 * Mensagem do 422 — **uma só**, cobrindo senha errada e código errado.
 *
 * A API responde o mesmo para os dois de propósito, e aqui a razão é ainda mais
 * direta que na desativação: o que sai desta rota é uma lista de credenciais
 * que *pulam* o segundo fator. Dizer qual metade falhou entregaria a quem está
 * com a sessão de outra pessoa exatamente o que ainda lhe falta.
 */
const MENSAGEM_RECUSADO =
  "Senha ou código não conferem. A API não informa qual dos dois falhou — confira os dois e tente de novo.";

const MENSAGEM_CAMPOS = "Informe a senha e o código do aplicativo autenticador.";
const MENSAGEM_INESPERADA = "Falha inesperada ao emitir os códigos. Tente novamente.";

function mensagemDeErro(erro: unknown): string {
  if (!(erro instanceof ApiError)) {
    return MENSAGEM_INESPERADA;
  }
  if (erro.status === 422) {
    return MENSAGEM_RECUSADO;
  }
  // 429 e erro de rede chegam com a mensagem da própria API (ou a do cliente),
  // e ela já é a certa — enriquecer aqui só a afastaria da causa real.
  return erro.message;
}

/**
 * Emitir uma lista nova de códigos de recuperação, sem desligar o segundo fator.
 *
 * A API exige senha **e** código atual, e não é excesso de zelo: o que volta
 * desta chamada entra no login no lugar do aplicativo autenticador. Uma sessão
 * sequestrada que pudesse pedir códigos novos viraria acesso permanente à
 * conta, imune à troca de senha e ao próprio MFA.
 *
 * **O aviso vem antes da confirmação, não depois.** Emitir a lista nova mata a
 * antiga inteira — inclusive os códigos que nunca foram usados. Quem guardou a
 * lista velha num cofre, num gerenciador de senhas ou num papel dentro da
 * carteira precisa saber, *antes* de clicar, que ela vira papel sem valor. Por
 * isso o texto está do lado do botão que submete, com confirmação marcada à
 * mão, e não numa tela anterior que a pessoa já esqueceu.
 *
 * O segredo TOTP **não** muda: o aplicativo cadastrado continua valendo e não
 * há QR code novo para escanear.
 */
export function FormularioReemitirCodigos({
  onEmitidos,
  onNaoAtivado,
  onCancelar,
}: {
  onEmitidos: (codigos: string[]) => void;
  onNaoAtivado: () => void;
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
      const { codigos } = await reemitirCodigosMfa(API_BASE_URL, { senha, codigo });
      // Daqui em diante estes códigos existem em um lugar só: este array, nesta
      // aba. Quem recebe é responsável por mostrá-los e descartá-los.
      onEmitidos(codigos);
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
        // O segundo fator não estava ativado — não há lista a reemitir. Como
        // `GET /api/auth/eu` não expõe esse campo, este 409 é informação nova, e
        // não erro de quem digitou.
        onNaoAtivado();
        return;
      }
      setErro(mensagemDeErro(causa));
    }
  }

  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>Emitir códigos de recuperação novos</h2>
        <span className="state state--2">Invalida os códigos atuais</span>
      </div>

      <div className="grid gap-4">
        <p className="text-sm leading-6 text-muted">
          A lista nova substitui a atual por inteiro. O segredo do aplicativo autenticador não muda:
          nada precisa ser cadastrado de novo, e o login segue pedindo os mesmos seis dígitos.
        </p>

        {/* O `<span>` não é enfeite: `.alert--error` é um flex container, e um
            `<strong>` solto viraria uma coluna à parte no meio da frase. */}
        <p className="alert--error">
          <span>
            <strong>Os seus códigos de recuperação atuais deixam de valer na hora</strong> —
            inclusive os que você nunca usou. Se você guardou a lista em um papel, num cofre ou no
            gerenciador de senhas, ela vira papel sem valor assim que você confirmar aqui.
          </span>
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
            <p className="text-xs text-muted">
              Os seis dígitos do aplicativo — um código de recuperação não serve aqui.
            </p>
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
            Entendo que os meus códigos de recuperação atuais deixam de valer.
          </label>

          {erro && (
            <p role="alert" className="alert--error">
              {erro}
            </p>
          )}

          <div className="flex flex-wrap gap-2">
            <button type="submit" disabled={enviando || !ciente} className="btn btn--primary">
              <KeyRound size={16} />
              {enviando ? "Emitindo…" : "Emitir códigos novos"}
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
          {/* A consequência é real e não aparece em lugar nenhum se não for dita
              aqui: a API conta as falhas desta rota em `tentativas_login`, com o
              e-mail da pessoa — as mesmas linhas que trancam o login. */}
          <p className="text-xs text-muted">
            Errar a senha ou o código várias vezes seguidas bloqueia temporariamente o login desta
            conta, como acontece na tela de entrada.
          </p>
        </form>
      </div>
    </section>
  );
}
