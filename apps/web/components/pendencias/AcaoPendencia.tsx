"use client";

import { useRouter } from "next/navigation";
import { useId, useState, useTransition } from "react";
import { ApiError, atualizarPendencia } from "@homecareos/contracts";
import type { PendenciaStatus } from "@homecareos/contracts";
import { API_BASE_URL } from "@/lib/env";
import { useAvisoDaFila } from "./AvisosDaFila";
import { proximoStatus, ROTULO_DE_STATUS } from "./filtros";

const ROTULO_DA_ACAO: Record<PendenciaStatus, string> = {
  aberta: "—",
  em_correcao: "Iniciar correção",
  resolvida: "Resolver",
};

/**
 * A segunda pessoa a clicar na mesma pendência recebe 422, porque o status já
 * mudou no banco. Isto **não é erro de quem clicou**: é a fila funcionando como
 * fila. A mensagem diz o que houve e a lista é recarregada, para a pessoa
 * decidir sobre o estado real em vez de repetir a tentativa contra um estado
 * que não existe mais.
 */
function mensagemDeConcorrencia(nome: string): string {
  return `“${nome}” mudou enquanto você olhava: outra pessoa já a transicionou. A lista foi atualizada com o estado atual.`;
}

function mensagemDeSumico(nome: string): string {
  return `“${nome}” não existe mais na API. A lista foi atualizada.`;
}

/**
 * Esconder o botão para o gestor é ergonomia; a autoridade é a API, que responde
 * 403 (ADR 0001: o gestor lê a operação e não faz conferência). Este texto é o
 * que a pessoa vê quando as duas discordam — papel alterado no servidor no meio
 * do turno, ou aba aberta desde antes da mudança.
 */
const MENSAGEM_SEM_PERMISSAO =
  "Seu papel não permite transicionar pendências: a conferência é feita por conferente ou coordenador. Nada foi alterado.";

const MENSAGEM_INESPERADA = "Falha inesperada ao transicionar a pendência. Tente novamente.";

type Estado =
  | { tipo: "ocioso" }
  | { tipo: "confirmando" }
  | { tipo: "erro"; mensagem: string }
  | { tipo: "negado"; mensagem: string };

/**
 * A ação de avançar uma pendência no ciclo `aberta → em_correcao → resolvida`.
 *
 * Só aparece para quem pode transicionar e para pendência que tem próxima
 * etapa — `resolvida` não tem, e a API não desfaz.
 *
 * **Resolver pede confirmação; iniciar a correção não.** A assimetria é
 * deliberada: só a primeira é irreversível pela API, e confirmar cada passo
 * treina a pessoa a clicar em "sim" sem ler, justamente o que estraga a
 * confirmação que importa.
 */
export function AcaoPendencia({
  pendenciaId,
  status,
  nome,
  responsavelAtual,
}: {
  pendenciaId: string;
  status: PendenciaStatus;
  /** Como a pendência é nomeada na confirmação e nos avisos. */
  nome: string;
  responsavelAtual: string;
}) {
  const router = useRouter();
  const avisar = useAvisoDaFila();
  const responsavelId = useId();
  const [responsavel, setResponsavel] = useState("");
  const [enviando, setEnviando] = useState(false);
  const [recarregando, iniciarRecarga] = useTransition();
  const [estado, setEstado] = useState<Estado>({ tipo: "ocioso" });

  const alvo = proximoStatus(status);
  if (alvo === null) {
    return null;
  }

  const ocupado = enviando || recarregando;

  function recarregarLista() {
    // Dentro da transição para o botão continuar desabilitado até a lista
    // voltar do servidor: reabilitá-lo antes convida ao segundo clique, que
    // agora só pode dar 422.
    iniciarRecarga(() => {
      router.refresh();
    });
  }

  async function transicionar(destino: PendenciaStatus) {
    setEstado({ tipo: "ocioso" });
    avisar(null);
    setEnviando(true);

    const informado = responsavel.trim();
    try {
      await atualizarPendencia(API_BASE_URL, pendenciaId, {
        status: destino,
        // Só quando há texto: ausente significa "não mexer no responsável
        // atual", enquanto uma string vazia apagaria o rótulo que já existe.
        ...(informado === "" ? {} : { responsavel: informado }),
      });
      setResponsavel("");
      setEnviando(false);
      // O PATCH pode ter movido o documento e disparado a revalidação; nada
      // disso está na resposta. Quem tem o retrato certo é o servidor.
      recarregarLista();
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

      if (causa instanceof ApiError && causa.status === 403) {
        // Insistir não muda nada: a recusa é do papel, não do momento. O botão
        // sai de cena e fica só a explicação.
        setEstado({ tipo: "negado", mensagem: MENSAGEM_SEM_PERMISSAO });
        return;
      }
      if (causa instanceof ApiError && (causa.status === 422 || causa.status === 404)) {
        avisar(
          causa.status === 422 ? mensagemDeConcorrencia(nome) : mensagemDeSumico(nome),
        );
        recarregarLista();
        return;
      }
      setEstado({
        tipo: "erro",
        mensagem: causa instanceof ApiError ? causa.message : MENSAGEM_INESPERADA,
      });
    }
  }

  if (estado.tipo === "negado") {
    return (
      <p role="alert" className="alert--error text-xs sm:max-w-56">
        {estado.mensagem}
      </p>
    );
  }

  if (estado.tipo === "confirmando") {
    return (
      <div className="grid gap-2 rounded-xl border border-red-200 bg-red-50 p-3 sm:w-56">
        <p className="text-xs leading-5 text-red-700">
          Resolver <strong>{nome}</strong>? A API não reabre pendência resolvida — não há como
          desfazer pela interface.
        </p>
        {responsavel.trim() !== "" && (
          <p className="text-xs text-red-700">
            Responsável a registrar: <strong>{responsavel.trim()}</strong>
          </p>
        )}
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="btn btn--primary h-9 min-h-9 px-3 text-xs"
            disabled={ocupado}
            onClick={() => void transicionar("resolvida")}
          >
            {ocupado ? "Resolvendo…" : "Confirmar resolução"}
          </button>
          <button
            type="button"
            className="btn btn--secondary h-9 min-h-9 px-3 text-xs"
            disabled={ocupado}
            onClick={() => setEstado({ tipo: "ocioso" })}
          >
            Cancelar
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="grid gap-2 sm:w-56">
      <div className="grid gap-1">
        <label htmlFor={responsavelId} className="text-[11px] font-semibold text-muted">
          Responsável (opcional)
        </label>
        <input
          id={responsavelId}
          type="text"
          className="field px-3 py-2 text-xs"
          value={responsavel}
          placeholder={responsavelAtual}
          disabled={ocupado}
          onChange={(evento) => setResponsavel(evento.target.value)}
        />
      </div>

      <button
        type="button"
        className="btn btn--primary h-9 min-h-9 px-3 text-xs"
        disabled={ocupado}
        onClick={() => {
          if (alvo === "resolvida") {
            setEstado({ tipo: "confirmando" });
            return;
          }
          void transicionar(alvo);
        }}
      >
        {ocupado ? "Enviando…" : ROTULO_DA_ACAO[alvo]}
      </button>
      <p className="text-[11px] text-muted">
        Passa para <strong>{ROTULO_DE_STATUS[alvo]}</strong>.
      </p>

      {estado.tipo === "erro" && (
        <p role="alert" className="alert--error text-xs">
          {estado.mensagem}
        </p>
      )}
    </div>
  );
}
