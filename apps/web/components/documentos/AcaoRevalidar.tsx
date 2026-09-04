"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { ApiError, revalidarDocumento } from "@homecareos/contracts";
import type { RevalidacaoResponse } from "@homecareos/contracts";
import { API_BASE_URL } from "@/lib/env";
import { ROTULO_DE_STATUS_DOCUMENTO, varianteDeStatus } from "./vocabulario";

/**
 * Esconder o botão para o gestor é ergonomia; a autoridade é a API, que responde
 * 403 (ADR 0001: o gestor lê a operação e não faz conferência). Este texto é o
 * que a pessoa vê quando as duas discordam — papel alterado no servidor no meio
 * do turno, ou aba aberta desde antes da mudança.
 */
const MENSAGEM_SEM_PERMISSAO =
  "Seu papel não permite revalidar documentos: a conferência é feita por conferente ou coordenador. Nada foi alterado.";

const MENSAGEM_SUMICO =
  "Este documento não existe mais na API. Volte para a listagem para ver o estado atual.";

const MENSAGEM_INESPERADA = "Falha inesperada ao revalidar o documento. Tente novamente.";

type Estado =
  | { tipo: "ocioso" }
  | { tipo: "sucesso"; resultado: RevalidacaoResponse }
  | { tipo: "indisponivel"; mensagem: string }
  | { tipo: "erro"; mensagem: string }
  | { tipo: "negado"; mensagem: string };

/**
 * Revalidar: reaplicar as regras ativas da operadora sobre a extração que já
 * existe.
 *
 * **Sem confirmação, de propósito.** A ação é repetível e não destrói nada — o
 * documento pode voltar de `resolvido` para `problema`, mas isso é a regra
 * reprovando de novo, não um dado perdido. Confirmar cada passo treina a pessoa
 * a clicar em "sim" sem ler, justamente o que estraga a confirmação que importa
 * (resolver uma pendência, em `AcaoPendencia`). O que a ação faz está escrito
 * embaixo do botão, antes do clique.
 */
export function AcaoRevalidar({ documentoId }: { documentoId: string }) {
  const router = useRouter();
  const [enviando, setEnviando] = useState(false);
  const [recarregando, iniciarRecarga] = useTransition();
  const [estado, setEstado] = useState<Estado>({ tipo: "ocioso" });

  const ocupado = enviando || recarregando;

  async function revalidar() {
    setEstado({ tipo: "ocioso" });
    setEnviando(true);

    try {
      const resultado = await revalidarDocumento(API_BASE_URL, documentoId);
      setEstado({ tipo: "sucesso", resultado });
      setEnviando(false);
      // A resposta diz o status e quantas pendências ficaram abertas, e nada
      // mais: as validações novas, a extração e o `updated_at` estão no
      // servidor. Dentro da transição para o botão continuar desabilitado até a
      // página voltar — reabilitá-lo antes convida ao segundo clique, que
      // rodaria as regras de novo sobre o mesmo dado.
      iniciarRecarga(() => {
        router.refresh();
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

      if (causa instanceof ApiError && causa.status === 403) {
        // Insistir não muda nada: a recusa é do papel, não do momento. O botão
        // sai de cena e fica só a explicação.
        setEstado({ tipo: "negado", mensagem: MENSAGEM_SEM_PERMISSAO });
        return;
      }
      if (causa instanceof ApiError && causa.status === 409) {
        // 409 e não 422: o pedido está correto, é o estado do documento que
        // impede revalidar agora — sem operadora, sem extração, extração
        // ilegível, operadora sem regra ativa, ou status terminal. A frase é da
        // API, e é ela que diz qual dos casos é; reescrevê-la aqui obrigaria a
        // adivinhar a causa pelo texto.
        setEstado({ tipo: "indisponivel", mensagem: causa.message });
        return;
      }
      if (causa instanceof ApiError && causa.status === 404) {
        setEstado({ tipo: "erro", mensagem: MENSAGEM_SUMICO });
        iniciarRecarga(() => {
          router.refresh();
        });
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
      <p role="alert" className="alert--error">
        {estado.mensagem}
      </p>
    );
  }

  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          className="btn btn--primary w-fit"
          disabled={ocupado}
          onClick={() => void revalidar()}
        >
          {ocupado ? "Revalidando…" : "Revalidar documento"}
        </button>
        <p className="m-0 max-w-md text-xs leading-5 text-muted">
          Reaplica as regras ativas da operadora sobre a extração que já existe — não extrai o
          documento de novo. Pode reprovar outra vez e devolver o documento para{" "}
          <strong className="text-ink">problema</strong> ou{" "}
          <strong className="text-ink">incompleto</strong>.
        </p>
      </div>

      {estado.tipo === "sucesso" && (
        <div role="status" className="alert--info items-center gap-3 text-sm">
          <span>
            Revalidado. O documento está em{" "}
            <span className={`state ${varianteDeStatus(estado.resultado.status)}`}>
              {ROTULO_DE_STATUS_DOCUMENTO[estado.resultado.status]}
            </span>{" "}
            com{" "}
            <strong className="text-ink">
              {estado.resultado.pendencias_abertas}{" "}
              {estado.resultado.pendencias_abertas === 1
                ? "pendência aberta"
                : "pendências abertas"}
            </strong>
            .
          </span>
        </div>
      )}

      {estado.tipo === "indisponivel" && (
        <p role="status" className="alert--info">
          <span>
            Não há revalidação possível agora: {estado.mensagem}. Tentar de novo não muda esse
            estado — ele se resolve vinculando a operadora, reenviando o documento ou cadastrando a
            regra, conforme o caso.
          </span>
        </p>
      )}

      {estado.tipo === "erro" && (
        <p role="alert" className="alert--error">
          {estado.mensagem}
        </p>
      )}
    </div>
  );
}
