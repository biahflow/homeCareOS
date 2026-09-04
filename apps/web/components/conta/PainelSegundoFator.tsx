"use client";

import { ArrowLeft, KeyRound, QrCode, RefreshCw, ShieldCheck, ShieldOff } from "lucide-react";
import { useRouter } from "next/navigation";
import { useId, useState } from "react";
import { ApiError, confirmarMfa, iniciarMfa } from "@homecareos/contracts";
import type { MfaIniciarOut } from "@homecareos/contracts";
import { API_BASE_URL } from "@/lib/env";
import { CodigosRecuperacao } from "./CodigosRecuperacao";
import { FormularioDesativarMfa } from "./FormularioDesativarMfa";
import { FormularioReemitirCodigos } from "./FormularioReemitirCodigos";
import { QrCodeOtpauth } from "./QrCodeOtpauth";

/**
 * Mensagem do 422 de `confirmar`, e ela nomeia as **duas** causas porque quem
 * digitou não tem como distingui-las — e a segunda não ocorre a ninguém.
 *
 * `POST /mfa/iniciar` substitui o segredo a cada chamada. Se a mesma conta
 * recomeçou o cadastro em outra aba ou em outro aparelho, o QR desta tela
 * morreu em silêncio, e o código que o aplicativo mostra nunca mais vai valer.
 * Dizer só "código incorreto" mandaria a pessoa digitar de novo, para sempre,
 * um código correto que a API não pode aceitar.
 */
const MENSAGEM_CODIGO_RECUSADO =
  "Código não aceito. Ele pode estar incorreto — os seis dígitos mudam a cada 30 segundos — ou " +
  "o cadastro pode ter sido recomeçado em outra aba ou em outro aparelho, o que invalida o QR " +
  "code desta tela. Se foi isso, gere um QR code novo e escaneie de novo.";

const MENSAGEM_CAMPO_CODIGO = "Informe o código do aplicativo autenticador.";
const MENSAGEM_INESPERADA_INICIAR = "Falha inesperada ao iniciar o cadastro. Tente novamente.";
const MENSAGEM_INESPERADA_CONFIRMAR = "Falha inesperada ao confirmar o código. Tente novamente.";

function mensagemDoIniciar(erro: unknown): string {
  // Erro de rede chega como `ApiError` com `status: 0` e mensagem própria do
  // cliente — ela já é a certa, e não deve virar "falha inesperada".
  return erro instanceof ApiError ? erro.message : MENSAGEM_INESPERADA_INICIAR;
}

function mensagemDoConfirmar(erro: unknown): string {
  if (!(erro instanceof ApiError)) {
    return MENSAGEM_INESPERADA_CONFIRMAR;
  }
  if (erro.status === 422) {
    return MENSAGEM_CODIGO_RECUSADO;
  }
  return erro.message;
}

const AVISO_JA_ATIVO =
  "O segundo fator já estava ativado nesta conta — provavelmente em outra aba ou em outro " +
  "aparelho. Nada foi alterado.";
const AVISO_NAO_ATIVO = "O segundo fator não está ativado nesta conta. Nada foi alterado.";
const AVISO_DESATIVADO =
  "Segundo fator desativado. O próximo login vai pedir apenas a senha, e os códigos de " +
  "recuperação anteriores deixaram de valer.";
const AVISO_CODIGOS_REEMITIDOS =
  "Códigos de recuperação novos emitidos. Os anteriores deixaram de valer — inclusive os que " +
  "você nunca usou. O aplicativo autenticador continua o mesmo.";

/**
 * Onde a tela está — e note que **não existe um passo "inativo"**.
 *
 * `GET /api/auth/eu` não devolve `mfa_ativado`, então a primeira renderização
 * não tem como saber se a conta já usa o segundo fator. `inicial` é esse não
 * saber, declarado: a tela oferece os caminhos e aprende o estado pela resposta
 * da API (409 em `iniciar` = já ativado; 409 em `desativar` = não ativado).
 * Inventar um campo que a API não expõe criaria uma segunda fonte de verdade
 * para divergir dela.
 */
type Passo =
  | { nome: "inicial" }
  /** `credencial: null` é quem voltou a esta tela **sem** um QR novo — ver `handleJaEscaneei`. */
  | { nome: "cadastrando"; credencial: MfaIniciarOut | null }
  /**
   * `origem` existe só para o selo da tela de códigos dizer a verdade: depois
   * de uma reemissão o segundo fator não *acabou* de ser ativado — ele já
   * estava, e anunciar ativação diria à pessoa que algo mudou no login dela.
   */
  | { nome: "codigos"; codigos: string[]; origem: "ativacao" | "reemissao" }
  | { nome: "ativo" }
  | { nome: "reemitindo" }
  | { nome: "desativando"; voltarPara: "inicial" | "ativo" };

/**
 * Cadastro, reemissão dos códigos de recuperação e desativação do segundo fator.
 *
 * A reemissão (issue #39) só aparece no passo `ativo`, e não no `inicial`, pela
 * mesma razão que o passo `inicial` existe: `GET /api/auth/eu` não devolve
 * `mfa_ativado`, então a tela só sabe que a conta usa o segundo fator depois de
 * a API dizer. Oferecer "emitir códigos novos" antes disso levaria a maioria a
 * um 409.
 *
 * **Client Component, e nada aqui roda no carregamento.** As duas coisas são a
 * mesma decisão: `POST /mfa/iniciar` substitui o segredo a cada chamada, então
 * ela só pode acontecer por clique explícito. Num `useEffect` de montagem, ou
 * no render de um Server Component, cada recarga da página invalidaria o QR
 * code que a pessoa acabou de escanear — e ela descobriria isso como um 422 na
 * confirmação, sem nenhuma pista da causa. O segredo também não teria o que
 * fazer num payload de Server Component, que viaja no HTML e fica no cache de
 * rotas do cliente.
 */
export function PainelSegundoFator() {
  const router = useRouter();
  const codigoId = useId();
  const [passo, setPasso] = useState<Passo>({ nome: "inicial" });
  const [ocupado, setOcupado] = useState(false);
  const [erro, setErro] = useState<string | null>(null);
  const [aviso, setAviso] = useState<string | null>(null);

  function limpar() {
    setErro(null);
    setAviso(null);
  }

  /** A sessão acabou no servidor: mesmo caminho do resto da área logada. */
  function voltarParaLogin() {
    router.replace("/login?motivo=sessao-encerrada");
    router.refresh();
  }

  /**
   * Gera (ou **substitui**) o segredo. Só é chamada por clique — nunca no
   * carregamento, nunca em efeito de montagem.
   */
  async function handleIniciar() {
    limpar();
    setOcupado(true);
    try {
      const credencial = await iniciarMfa(API_BASE_URL);
      setPasso({ nome: "cadastrando", credencial });
      setOcupado(false);
    } catch (causa) {
      if (causa instanceof ApiError && causa.status === 401) {
        voltarParaLogin();
        return;
      }
      setOcupado(false);
      if (causa instanceof ApiError && causa.status === 409) {
        // 409 não é falha: é a API dizendo que o segundo fator já está ativado,
        // e é a única forma de esta tela descobrir isso. A recusa em substituir
        // o segredo de quem já usa MFA é proteção — uma sessão sequestrada
        // trocaria a credencial sem provar nada.
        setPasso({ nome: "ativo" });
        setAviso(AVISO_JA_ATIVO);
        return;
      }
      setErro(mensagemDoIniciar(causa));
    }
  }

  /**
   * O caminho de quem recarregou a página no meio do cadastro.
   *
   * Sem ele, a única ação visível seria "Ativar segundo fator" — que substitui
   * o segredo e joga fora exatamente o que o aplicativo já guardou. Aqui não há
   * chamada nenhuma à API: o segredo continua gravado no servidor, e confirmar
   * o código é o que falta.
   */
  function handleJaEscaneei() {
    limpar();
    setPasso({ nome: "cadastrando", credencial: null });
  }

  async function handleConfirmar(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const dados = new FormData(event.currentTarget);
    const codigo = String(dados.get("codigo") ?? "").trim();

    if (codigo === "") {
      setErro(MENSAGEM_CAMPO_CODIGO);
      return;
    }

    limpar();
    setOcupado(true);
    try {
      const { codigos } = await confirmarMfa(API_BASE_URL, { codigo });
      // A partir daqui o segundo fator está ativado e estes códigos existem em
      // um lugar só: este array, nesta aba. Não há endpoint que os mostre de novo.
      setPasso({ nome: "codigos", codigos, origem: "ativacao" });
      setOcupado(false);
    } catch (causa) {
      if (causa instanceof ApiError && causa.status === 401) {
        voltarParaLogin();
        return;
      }
      setOcupado(false);
      setErro(mensagemDoConfirmar(causa));
    }
  }

  return (
    <div className="grid gap-4">
      {aviso && (
        <p role="status" className="alert--info">
          {aviso}
        </p>
      )}

      {passo.nome === "inicial" && (
        <section className="panel">
          <div className="panel-heading">
            <h2>Verificação em duas etapas</h2>
            <span className="state state--off">Estado não informado</span>
          </div>

          <div className="grid gap-4">
            <p className="text-sm leading-6 text-muted">
              Com o segundo fator ativado, entrar exige a senha <strong>e</strong> um código de
              seis dígitos gerado por um aplicativo autenticador no seu celular (Google
              Authenticator, Authy, 1Password). Uma senha vazada, sozinha, deixa de abrir a conta.
            </p>

            <p className="alert--info">
              Esta tela não consegue saber, antes de você agir, se o segundo fator já está ativado
              nesta conta — a API não informa esse estado. Se ele já estiver ativo, o botão abaixo
              avisa, em vez de recomeçar o cadastro.
            </p>

            {erro && (
              <p role="alert" className="alert--error">
                {erro}
              </p>
            )}

            <button
              type="button"
              onClick={handleIniciar}
              disabled={ocupado}
              className="btn btn--primary w-fit"
            >
              <ShieldCheck size={16} />
              {ocupado ? "Gerando…" : "Ativar segundo fator"}
            </button>

            <div className="grid gap-2 border-t border-line pt-4">
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={handleJaEscaneei}
                  disabled={ocupado}
                  className="btn btn--secondary"
                >
                  <QrCode size={16} />
                  Já escaneei o QR code
                </button>
                <button
                  type="button"
                  onClick={() => {
                    limpar();
                    setPasso({ nome: "desativando", voltarPara: "inicial" });
                  }}
                  disabled={ocupado}
                  className="btn btn--ghost"
                >
                  <ShieldOff size={16} />
                  Desativar o segundo fator
                </button>
              </div>
              <p className="text-xs text-muted">
                Use <strong>Já escaneei o QR code</strong> se você recarregou a página no meio do
                cadastro: gerar um QR code novo substituiria o segredo que o seu aplicativo já
                guardou.
              </p>
            </div>
          </div>
        </section>
      )}

      {passo.nome === "cadastrando" && (
        <section className="panel">
          <div className="panel-heading">
            <h2>Cadastre no aplicativo autenticador</h2>
            <span className="state state--0">Passo 1 de 2</span>
          </div>

          <div className="grid gap-5">
            {passo.credencial ? (
              <div className="grid gap-5 sm:grid-cols-[auto_1fr] sm:items-start">
                <QrCodeOtpauth uri={passo.credencial.otpauth_uri} />
                <div className="grid gap-2">
                  <p className="text-sm leading-6 text-muted">
                    Abra o aplicativo autenticador, escolha adicionar uma conta e aponte a câmera
                    para o código ao lado.
                  </p>
                  <p className="text-sm font-semibold text-ink">
                    O seu aplicativo não lê QR code?
                  </p>
                  <p className="text-sm leading-6 text-muted">
                    Cadastre uma chave manual, do tipo baseado em tempo:
                  </p>
                  {/* A chave fica visível para ser digitada, e não há botão de
                      copiar: a área de transferência de uma estação
                      compartilhada sobrevive à tela, e ninguém precisa dela
                      para escanear o QR code ao lado. Quem realmente precisar
                      copiar seleciona o texto. */}
                  <code className="rounded-xl border border-line bg-canvas px-3 py-2 font-mono text-sm break-all text-ink">
                    {agruparSegredo(passo.credencial.secret)}
                  </code>
                  <p className="text-xs text-muted">
                    Os espaços servem só para conferir a digitação — o aplicativo os ignora.
                  </p>
                </div>
              </div>
            ) : (
              <p className="alert--info">
                O QR code não está mais nesta tela: ele existe apenas enquanto a página fica
                aberta. O segredo que o seu aplicativo guardou continua valendo no servidor —
                digite abaixo o código que ele mostra. Se você não chegou a escanear nada, gere um
                QR code novo.
              </p>
            )}

            {/* O `<span>` não é enfeite: `.alert--info` é um flex container, e um
                `<strong>` solto viraria uma coluna à parte no meio da frase. */}
            <p className="alert--info">
              <span>
                Ao confirmar, o segundo fator é ativado e a tela seguinte mostra os seus códigos de
                recuperação <strong>uma única vez</strong>, sem como pedi-los de novo. Tenha onde
                guardá-los antes de continuar.
              </span>
            </p>

            <form className="form-grid" onSubmit={handleConfirmar} noValidate>
              <div className="grid max-w-xs gap-1.5">
                <label htmlFor={codigoId} className="form-label">
                  Código do aplicativo
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
                  disabled={ocupado}
                  autoFocus
                />
                <p className="text-xs text-muted">Os seis dígitos mudam a cada 30 segundos.</p>
              </div>

              {erro && (
                <p role="alert" className="alert--error">
                  {erro}
                </p>
              )}

              <div className="flex flex-wrap gap-2">
                <button type="submit" disabled={ocupado} className="btn btn--primary">
                  {ocupado ? "Confirmando…" : "Confirmar e ativar"}
                </button>
                <button
                  type="button"
                  onClick={handleIniciar}
                  disabled={ocupado}
                  className="btn btn--secondary"
                >
                  <RefreshCw size={16} />
                  Gerar um QR code novo
                </button>
                <button
                  type="button"
                  onClick={() => {
                    limpar();
                    setPasso({ nome: "inicial" });
                  }}
                  disabled={ocupado}
                  className="btn btn--ghost"
                >
                  <ArrowLeft size={16} />
                  Voltar
                </button>
              </div>
              <p className="text-xs text-muted">
                Gerar um QR code novo <strong>substitui</strong> o segredo no servidor: o código
                desta tela, e o que o seu aplicativo já guardou, deixam de valer na hora.
              </p>
            </form>
          </div>
        </section>
      )}

      {passo.nome === "codigos" && (
        <CodigosRecuperacao
          codigos={passo.codigos}
          selo={passo.origem === "reemissao" ? "Códigos substituídos" : undefined}
          onConcluir={() => {
            // Trocar de passo é o que descarta o array: os códigos não vão para
            // lugar nenhum além desta renderização, e não há caminho de volta.
            setPasso({ nome: "ativo" });
          }}
        />
      )}

      {passo.nome === "ativo" && (
        <section className="panel">
          <div className="panel-heading">
            <h2>Verificação em duas etapas</h2>
            <span className="state state--1">Ativada</span>
          </div>

          <div className="grid gap-4">
            <p className="text-sm leading-6 text-muted">
              O login desta conta pede a senha e, em seguida, o código de seis dígitos do
              aplicativo autenticador. Sem o celular à mão, use um dos códigos de recuperação.
            </p>

            {/* O `<span>` não é enfeite: `.alert--info` é um flex container, e um
                `<strong>` solto viraria uma coluna à parte no meio da frase. */}
            <p className="alert--info">
              <span>
                Perdeu a lista de códigos de recuperação, ou acha que ela pode ter sido vista por
                outra pessoa? Emita uma lista nova com a sua senha e o código do aplicativo — o
                aplicativo autenticador continua o mesmo, e{" "}
                <strong>os códigos atuais deixam de valer</strong>.
              </span>
            </p>

            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => {
                  limpar();
                  setPasso({ nome: "reemitindo" });
                }}
                className="btn btn--secondary"
              >
                <KeyRound size={16} />
                Emitir códigos novos
              </button>
              <button
                type="button"
                onClick={() => {
                  limpar();
                  setPasso({ nome: "desativando", voltarPara: "ativo" });
                }}
                className="btn btn--ghost"
              >
                <ShieldOff size={16} />
                Desativar o segundo fator
              </button>
            </div>
          </div>
        </section>
      )}

      {passo.nome === "reemitindo" && (
        <FormularioReemitirCodigos
          onEmitidos={(codigos) => {
            // O aviso sobe junto com a lista nova: ele fica visível acima do
            // painel de códigos e sobrevive ao "Concluir", que é quando a
            // pessoa precisa lembrar do que acabou de perder valor.
            setPasso({ nome: "codigos", codigos, origem: "reemissao" });
            setAviso(AVISO_CODIGOS_REEMITIDOS);
          }}
          onNaoAtivado={() => {
            setPasso({ nome: "inicial" });
            setAviso(AVISO_NAO_ATIVO);
          }}
          onCancelar={() => {
            limpar();
            setPasso({ nome: "ativo" });
          }}
        />
      )}

      {passo.nome === "desativando" && (
        <FormularioDesativarMfa
          onConcluido={(resultado) => {
            setPasso({ nome: "inicial" });
            setAviso(resultado === "desativado" ? AVISO_DESATIVADO : AVISO_NAO_ATIVO);
          }}
          onCancelar={() => {
            limpar();
            setPasso({ nome: passo.voltarPara });
          }}
        />
      )}
    </div>
  );
}

/**
 * O segredo em blocos de quatro, só para exibição.
 *
 * São 32 caracteres em base32 que alguém vai copiar com os olhos para um
 * aplicativo de desktop; em bloco corrido, perder a linha é questão de tempo.
 * O aplicativo ignora os espaços, e o valor original não é alterado em lugar
 * nenhum — esta função existe do lado de fora do dado.
 */
function agruparSegredo(secret: string): string {
  return (secret.match(/.{1,4}/g) ?? [secret]).join(" ");
}
