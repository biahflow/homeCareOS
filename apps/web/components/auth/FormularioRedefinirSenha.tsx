"use client";

import Link from "next/link";
import { useEffect, useId, useState } from "react";
import { ApiError, redefinirSenha } from "@homecareos/contracts";
import { API_BASE_URL } from "@/lib/env";

/**
 * Mensagem de sucesso. Precisa dizer que as sessões caíram — inclusive a
 * atual — porque `redefinir_senha` roda `sessoes.revogar_todas` no mesmo
 * commit da troca de senha (`auth/router.py`, `auth/recuperacao.py`): se a
 * conta foi comprometida, trocar a senha sem derrubar as sessões abertas do
 * invasor não resolveria nada. Não há sessão para manter depois disto — a
 * tela leva ao login, e não tenta manter ninguém autenticado.
 */
const MENSAGEM_SUCESSO =
  "Senha redefinida. Por segurança, encerramos todas as sessões abertas nesta conta — " +
  "inclusive esta, e as de qualquer outro dispositivo. Entre novamente com a nova senha.";

const MENSAGEM_CAMPOS = "Preencha a nova senha nos dois campos.";
// Comparação feita aqui porque a API só recebe uma senha, não duas para
// conferir — o erro de digitação numa senha que não se vê (dois campos
// `type="password"`) é o modo de falha comum, e nada no servidor consegue
// pegá-lo.
const MENSAGEM_SENHAS_DIFERENTES = "As senhas digitadas não conferem.";
const MENSAGEM_INESPERADA = "Falha ao redefinir a senha. Verifique sua conexão e tente novamente.";

/**
 * Mensagem do 422. A API devolve o mesmo `status` para token inválido e para
 * senha fraca — ver a docstring de `redefinirSenha` em `@homecareos/contracts`
 * e `api/errors.py:_tipo_do_status`. Só o texto que a própria API escreveu
 * distingue as duas, e é ele que exibimos: nenhuma heurística sobre a
 * mensagem, porque isso quebraria no dia em que o texto mudasse uma palavra.
 */
function mensagemDeErro(erro: unknown): string {
  if (!(erro instanceof ApiError)) {
    return MENSAGEM_INESPERADA;
  }
  return erro.message;
}

export function FormularioRedefinirSenha({ tokenInicial }: { tokenInicial?: string }) {
  const novaSenhaId = useId();
  const confirmarSenhaId = useId();

  // Capturado uma única vez, na primeira renderização deste componente — não
  // é relido da URL depois disso. É este valor, e não a URL, que sobrevive à
  // limpeza feita no efeito abaixo.
  const [token] = useState(tokenInicial ?? null);
  const [enviando, setEnviando] = useState(false);
  const [erro, setErro] = useState<string | null>(null);
  const [sucesso, setSucesso] = useState(false);

  useEffect(() => {
    // O token chega pela URL (`/redefinir-senha?token=...`) porque é assim
    // que o link do e-mail funciona, mas uma credencial de uso único não
    // deve morar na barra de endereço: ela vai para o histórico do
    // navegador, e esta é uma estação compartilhada entre turnos. Assim que
    // o valor já está capturado em memória (acima), a URL é limpa.
    //
    // Trade-off consciente: quem recarregar a página depois desta limpeza
    // perde o token da tela e precisa clicar de novo no link do e-mail. É
    // aceitável porque o token **não** foi consumido só por ter sido lido —
    // apenas um `submit` bem-sucedido o marca como usado
    // (`recuperacao.marcar_usado`, chamado depois da senha nova passar pela
    // validação de força) — e a alternativa seria deixar essa credencial
    // parada no histórico do navegador indefinidamente.
    window.history.replaceState(null, "", window.location.pathname);
  }, []);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (token === null) {
      return;
    }
    const dados = new FormData(event.currentTarget);
    const novaSenha = String(dados.get("nova_senha") ?? "");
    const confirmarSenha = String(dados.get("confirmar_senha") ?? "");

    if (novaSenha === "" || confirmarSenha === "") {
      setErro(MENSAGEM_CAMPOS);
      return;
    }
    if (novaSenha !== confirmarSenha) {
      setErro(MENSAGEM_SENHAS_DIFERENTES);
      return;
    }

    setErro(null);
    setEnviando(true);
    try {
      await redefinirSenha(API_BASE_URL, { token, nova_senha: novaSenha });
      setSucesso(true);
    } catch (causa) {
      // O formulário continua utilizável depois do erro, e de propósito: com
      // senha fraca o token ainda vale (a API valida a força antes de marcar
      // o token como usado), e quem errou só a senha precisa poder tentar de
      // novo aqui mesmo — sem navegar embora, sem desabilitar nada.
      setEnviando(false);
      setErro(mensagemDeErro(causa));
    }
  }

  if (token === null) {
    return (
      <>
        <div className="page-head">
          <p className="eyebrow">Redefinição de senha</p>
          <h1>Link inválido</h1>
          <p>
            Este endereço não traz um token de redefinição. Isso acontece se o link já foi
            usado, se esta página foi recarregada depois de aberta, ou se o endereço foi
            digitado à mão.
          </p>
        </div>
        <Link href="/esqueci-senha" className="btn btn--primary mt-2">
          Pedir um novo link
        </Link>
      </>
    );
  }

  if (sucesso) {
    return (
      <>
        <div className="page-head">
          <p className="eyebrow">Redefinição de senha</p>
          <h1>Senha redefinida</h1>
        </div>
        <p role="status" className="alert--info">
          {MENSAGEM_SUCESSO}
        </p>
        <Link href="/login" className="btn btn--primary mt-2">
          Ir para o login
        </Link>
      </>
    );
  }

  return (
    <>
      <div className="page-head">
        <p className="eyebrow">Redefinição de senha</p>
        <h1>Escolha uma nova senha</h1>
      </div>

      <form className="form-grid" onSubmit={handleSubmit} noValidate>
        <div className="grid gap-1.5">
          <label htmlFor={novaSenhaId} className="form-label">
            Nova senha
          </label>
          <input
            id={novaSenhaId}
            name="nova_senha"
            type="password"
            autoComplete="new-password"
            placeholder="••••••••"
            className="field"
            disabled={enviando}
          />
        </div>

        <div className="grid gap-1.5">
          <label htmlFor={confirmarSenhaId} className="form-label">
            Confirme a nova senha
          </label>
          <input
            id={confirmarSenhaId}
            name="confirmar_senha"
            type="password"
            autoComplete="new-password"
            placeholder="••••••••"
            className="field"
            disabled={enviando}
          />
        </div>

        <button type="submit" className="btn btn--primary mt-2" disabled={enviando}>
          {enviando ? "Redefinindo…" : "Redefinir senha"}
        </button>

        {erro && (
          <p role="alert" className="alert--error">
            {erro}
          </p>
        )}

        {/*
          Sempre visível, e não só depois de um erro: os dois 422 são
          indistinguíveis para este componente (ver `mensagemDeErro`), então
          não há como mostrar esta saída apenas quando o problema for
          "de fato" o token. Tratar todo 422 como "peça outro link" mandaria
          quem só errou a senha para um e-mail que pode nem sair (SMTP não
          configurado neste ambiente) — por isso o campo continua ao lado, e
          não no lugar, do link abaixo.
        */}
        <Link href="/esqueci-senha" className="btn btn--ghost">
          Pedir um novo link
        </Link>
      </form>
    </>
  );
}
