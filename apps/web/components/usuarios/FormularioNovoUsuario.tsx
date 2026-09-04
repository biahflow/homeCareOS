"use client";

import { Check, Copy, UserPlus } from "lucide-react";
import { useRouter } from "next/navigation";
import { useId, useState, useTransition } from "react";
import { ApiError, criarUsuario, detalhesDeValidacao } from "@homecareos/contracts";
import type { Papel, UsuarioOut } from "@homecareos/contracts";
import { API_BASE_URL } from "@/lib/env";
import { PAPEL_LABEL } from "@/components/shell/usuario";
import { PAPEIS_ATRIBUIVEIS } from "./filtros";

const MENSAGEM_CAMPOS = "Informe o nome e o e-mail.";
const MENSAGEM_INESPERADA = "Falha inesperada ao criar o usuário. Tente novamente.";

/**
 * O 401 é tratado navegando, não com texto; os demais viram lista de mensagens.
 *
 * O 422 é o único que não tem a frase útil em `message`: a API responde
 * "parâmetros inválidos" fixo e põe o que a regra disse em `detalhes` (ver
 * `detalhesDeValidacao`). Os outros — 403 do papel, 409 do e-mail, 503 do token
 * — já chegam com a frase pronta, e **saem como a API as escreveu**. O 409 em
 * especial é neutro de propósito: ele não diz de quem é a conta que já usa o
 * e-mail, e "melhorar" o texto para confirmar que a pessoa existe transformaria
 * a criação num oráculo de quem trabalha na operação.
 */
function mensagensDoErro(erro: unknown): string[] {
  if (!(erro instanceof ApiError)) {
    return [MENSAGEM_INESPERADA];
  }
  if (erro.status === 422) {
    const detalhes = detalhesDeValidacao(erro);
    return detalhes.length > 0 ? detalhes : [erro.message];
  }
  return [erro.message];
}

/**
 * O que se pode afirmar sobre a conta depois de uma falha.
 *
 * `nao-criada` quando o servidor **respondeu** recusando (403, 409, 422, 503) ou
 * quando nem chegamos a enviar: aí a ausência da conta é fato, e dizê-la poupa
 * o coordenador de procurar na lista o que não está lá.
 *
 * `incerto` quando a conexão caiu (`ApiError` com `status: 0`, ver `requisitar`
 * em `packages/contracts/src/cliente.ts`). O `fetch` estoura tanto no pedido que
 * não saiu quanto na resposta que não voltou — e no segundo caso a conta existe,
 * criada por um `POST` que chegou. Afirmar "nenhuma conta foi criada" aqui manda
 * a pessoa pelo caminho errado justamente no cenário caro: o token de definição
 * de senha só existia naquela resposta e não volta nunca mais, então o que
 * resta é a própria pessoa pedir um link em "Esqueci minha senha" — e insistir
 * no formulário só produz um 409.
 */
type DesfechoDaFalha = "nao-criada" | "incerto";

function desfechoDaFalha(erro: unknown): DesfechoDaFalha {
  return erro instanceof ApiError && erro.status === 0 ? "incerto" : "nao-criada";
}

type Estado =
  | { tipo: "ocioso" }
  | { tipo: "erro"; mensagens: string[]; desfecho: DesfechoDaFalha }
  // O token vive **aqui**, no estado deste componente, e em lugar nenhum além
  // daqui. Ver a docstring do componente.
  | { tipo: "criado"; usuario: UsuarioOut; token: string };

/**
 * Criação de usuário e a exibição **única** do token de definição de senha.
 *
 * `POST /api/usuarios` é o único lugar em que o token existe em claro: o banco
 * guarda apenas o SHA-256 dele, nenhum endpoint o mostra de novo, e ele vale
 * uma vez só e por tempo limitado. Tudo nesta tela sai daí:
 *
 * - **copiar**, porque transcrever um token à mão erra, e o erro só aparece na
 *   hora em que a outra pessoa tenta entrar;
 * - **confirmação explícita para sair**, porque um clique acidental custa o
 *   token inteiro — e a saída é de mão única, o componente descarta o estado;
 * - **nada persistido, nada logado, nada na URL**: o token vive no estado deste
 *   componente e morre com ele. Não vai para `console.log` (o console fica
 *   aberto em estação compartilhada e o histórico dele sobrevive à navegação),
 *   não vira query string (ela entra em histórico, log de proxy e header
 *   `Referer`), não vai para `localStorage` nem para atributo do DOM. Ele é
 *   texto dentro de um nó, e some quando o nó some.
 *
 * A lista só é recarregada **depois** de a pessoa concluir esta etapa: um
 * `router.refresh()` no instante do sucesso mexeria na árvore que segura o
 * token, e nenhuma economia de um clique paga esse risco.
 */
export function FormularioNovoUsuario() {
  const router = useRouter();
  const nomeId = useId();
  const emailId = useId();
  const papelId = useId();
  const confirmacaoId = useId();
  const [enviando, setEnviando] = useState(false);
  const [recarregando, iniciarRecarga] = useTransition();
  const [estado, setEstado] = useState<Estado>({ tipo: "ocioso" });
  const [confirmado, setConfirmado] = useState(false);
  const [copia, setCopia] = useState<"ocioso" | "copiado" | "falhou">("ocioso");

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const dados = new FormData(event.currentTarget);
    const nome = String(dados.get("nome") ?? "").trim();
    const email = String(dados.get("email") ?? "").trim();
    const papel = String(dados.get("papel") ?? "") as Papel;

    if (nome === "" || email === "") {
      setEstado({ tipo: "erro", mensagens: [MENSAGEM_CAMPOS], desfecho: "nao-criada" });
      return;
    }

    setEstado({ tipo: "ocioso" });
    setEnviando(true);
    try {
      const resposta = await criarUsuario(API_BASE_URL, { nome, email, papel });
      setEnviando(false);
      setConfirmado(false);
      setCopia("ocioso");
      setEstado({
        tipo: "criado",
        usuario: resposta.usuario,
        token: resposta.token_definicao_senha,
      });
    } catch (causa) {
      if (causa instanceof ApiError && causa.status === 401) {
        // A sessão acabou no servidor — mesmo tratamento do resto da área
        // logada: voltar ao login dizendo o que houve. `enviando` continua
        // ligado de propósito, a navegação já está em curso.
        router.replace("/login?motivo=sessao-encerrada");
        router.refresh();
        return;
      }

      setEnviando(false);
      setEstado({
        tipo: "erro",
        mensagens: mensagensDoErro(causa),
        // Em toda recusa **respondida pela API** a conta não existe — e no 503
        // isso é contraintuitivo, porque a criação chegou a acontecer antes de
        // a API desfazê-la junto com o token. Já a conexão que cai não permite
        // afirmar nada: ver `DesfechoDaFalha`.
        desfecho: desfechoDaFalha(causa),
      });
    }
  }

  async function handleCopiar(token: string) {
    try {
      await navigator.clipboard.writeText(token);
      setCopia("copiado");
    } catch {
      // Área de transferência bloqueada (permissão negada, contexto não
      // seguro). Dizer que copiou sem ter copiado é o pior desfecho possível
      // aqui: a pessoa concluiria a etapa confiando numa cópia que não existe,
      // e o token não volta.
      setCopia("falhou");
    }
  }

  function concluir() {
    // O estado sai antes do refresh: é o descarte do token, e ele não depende
    // de a recarga dar certo.
    setEstado({ tipo: "ocioso" });
    setConfirmado(false);
    setCopia("ocioso");
    iniciarRecarga(() => {
      router.refresh();
    });
  }

  if (estado.tipo === "criado") {
    return (
      <section className="panel">
        <div className="panel-heading">
          <h2>Usuário criado — guarde o token agora</h2>
          <span className="state state--1">{PAPEL_LABEL[estado.usuario.papel]}</span>
        </div>

        <div className="grid gap-4">
          {/* O `<span>` não é enfeite: `.alert--error` é um flex container, e um
              `<strong>` solto viraria uma coluna à parte no meio da frase. */}
          <p role="alert" className="alert--error">
            <span>
              <strong>Esta é a única vez que este token aparece.</strong> Não há como pedi-lo de
              novo: o sistema guarda apenas uma versão embaralhada dele. Se você sair desta tela sem
              copiá-lo, o caminho passa a ser a própria pessoa pedir um link em{" "}
              <strong>Esqueci minha senha</strong>, na tela de entrada.
            </span>
          </p>

          <p className="m-0 text-sm leading-6 text-muted">
            <strong className="text-ink">{estado.usuario.nome}</strong> ({estado.usuario.email})
            ainda não tem senha — ninguém tem, nem você. Com este token ela define a própria senha
            em <strong className="text-ink">Redefinir senha</strong>. O token vale por tempo
            limitado e uma única vez.
          </p>

          <p className="m-0 rounded-xl border border-line bg-canvas p-4 text-center font-mono text-sm break-all text-ink">
            <code>{estado.token}</code>
          </p>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => void handleCopiar(estado.token)}
              className="btn btn--secondary"
            >
              {copia === "copiado" ? <Check size={16} /> : <Copy size={16} />}
              {copia === "copiado" ? "Copiado" : "Copiar token"}
            </button>
          </div>

          {copia === "falhou" && (
            <p role="alert" className="alert--error">
              Não foi possível copiar automaticamente. Selecione o token acima e copie manualmente
              antes de concluir.
            </p>
          )}

          <p className="alert--info">
            Entregue o token à pessoa por um canal que só ela lê. Quem tiver este texto define a
            senha da conta — ele vale tanto quanto a senha até ser usado.
          </p>

          <label htmlFor={confirmacaoId} className="flex items-start gap-2.5 text-sm text-ink">
            <input
              id={confirmacaoId}
              type="checkbox"
              checked={confirmado}
              onChange={(evento) => setConfirmado(evento.target.checked)}
              className="mt-0.5 size-4 accent-brand-500"
            />
            Copiei o token e vou entregá-lo a {estado.usuario.nome}.
          </label>

          <button
            type="button"
            onClick={concluir}
            disabled={!confirmado || recarregando}
            className="btn btn--primary w-fit"
          >
            {recarregando ? "Atualizando…" : "Concluir"}
          </button>
        </div>
      </section>
    );
  }

  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>Novo usuário</h2>
      </div>

      <form className="form-grid" onSubmit={handleSubmit} noValidate>
        <div className="grid gap-4 sm:grid-cols-3">
          <div className="grid gap-1.5">
            <label htmlFor={nomeId} className="form-label">
              Nome
            </label>
            <input
              id={nomeId}
              name="nome"
              type="text"
              autoComplete="off"
              placeholder="Nome de quem vai usar a conta"
              className="field"
              disabled={enviando}
            />
          </div>

          <div className="grid gap-1.5">
            <label htmlFor={emailId} className="form-label">
              E-mail
            </label>
            <input
              id={emailId}
              name="email"
              type="email"
              autoComplete="off"
              placeholder="pessoa@empresa.com"
              className="field"
              disabled={enviando}
            />
          </div>

          <div className="grid gap-1.5">
            <label htmlFor={papelId} className="form-label">
              Papel
            </label>
            <select
              id={papelId}
              name="papel"
              className="field"
              defaultValue="conferente"
              disabled={enviando}
            >
              {PAPEIS_ATRIBUIVEIS.map((papel) => (
                <option key={papel} value={papel}>
                  {PAPEL_LABEL[papel]}
                </option>
              ))}
            </select>
          </div>
        </div>

        <p className="m-0 text-xs leading-5 text-muted">
          A conta nasce <strong className="text-ink">sem senha conhecida por ninguém</strong>: o
          sistema devolve, uma única vez, um token com que a pessoa define a própria senha. O papel{" "}
          <strong className="text-ink">gestor</strong> não é criado por aqui — ele é criado por
          linha de comando, no servidor.
        </p>

        <button type="submit" className="btn btn--primary mt-2 w-fit" disabled={enviando}>
          <UserPlus size={16} />
          {enviando ? "Criando…" : "Criar usuário"}
        </button>

        {estado.tipo === "erro" && (
          <div role="alert" className="alert--error">
            <span>
              {estado.mensagens.map((mensagem) => (
                <span key={mensagem} className="block">
                  {mensagem}
                </span>
              ))}
              {estado.desfecho === "nao-criada" ? (
                <span className="mt-1 block">
                  <strong>Nenhuma conta foi criada.</strong> Corrija os dados acima e envie de novo
                  — não há usuário novo para procurar na lista.
                </span>
              ) : (
                <span className="mt-1 block">
                  <strong>Não dá para saber se a conta foi criada:</strong> a conexão caiu antes da
                  resposta. Procure o e-mail na lista abaixo antes de enviar de novo. Se a conta
                  estiver lá, o token de definição de senha se perdeu com a resposta e não há como
                  reemiti-lo — peça à pessoa que use <strong>Esqueci minha senha</strong> na tela de
                  entrada.
                </span>
              )}
            </span>
          </div>
        )}
      </form>
    </section>
  );
}
