"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { ApiError, atualizarCanal, detalhesDeValidacao } from "@homecareos/contracts";
import type { CanalOut } from "@homecareos/contracts";
import { API_BASE_URL } from "@/lib/env";
import { rotuloDoCanal } from "./filtros";

const MENSAGEM_INESPERADA = "Falha inesperada ao alterar o canal. Tente novamente.";

/**
 * As recusas que importam aqui já chegam com a frase pronta da API — o 403 de
 * papel diz que a operação é do coordenador. O 422 é o único que não tem frase
 * útil em `message` (a API responde "parâmetros inválidos" fixo e põe o motivo
 * em `detalhes`), e ele é praticamente inalcançável: o canal vem da listagem
 * que a própria API devolveu. Tratado mesmo assim, porque o custo é uma linha e
 * a alternativa é a pessoa ler "parâmetros inválidos" numa tela sem parâmetros.
 */
function mensagemDoErro(erro: unknown): string {
  if (!(erro instanceof ApiError)) {
    return MENSAGEM_INESPERADA;
  }
  if (erro.status === 422) {
    const detalhes = detalhesDeValidacao(erro);
    return detalhes.length > 0 ? detalhes.join(" ") : erro.message;
  }
  return erro.message;
}

type Estado = { tipo: "fechado" } | { tipo: "confirmando" } | { tipo: "erro"; mensagem: string };

/**
 * Ligar e desligar um canal de alerta. Só o coordenador vê este componente —
 * quem decide isso é a página, e a autoridade é o 403 da API.
 *
 * **Desligar pede confirmação; ligar não.** A assimetria é deliberada, e é a
 * mesma de `AcaoUsuario`: confirmar cada passo treina a pessoa a clicar em
 * "sim" sem ler, justamente o que estraga a confirmação que importa. E só um
 * dos dois lados é perigoso — **quem desliga um canal silencia a operação**
 * (ADR 0006), e o estrago não aparece: a ausência de alerta é indistinguível de
 * "não havia o que alertar", que é o mesmo aviso que a tela de alertas já dá
 * sobre o cooldown. Ligar, no pior caso, não faz nada sair.
 *
 * **A confirmação tem duas forças.** Quando nenhum canal sobra enviando, o
 * texto muda: não é "este aviso para de sair", é "nenhum aviso sai por caminho
 * nenhum". Quem sabe disso é a página, que tem a lista inteira — ver
 * `desligarSilenciaTudo`.
 *
 * A frase forte descreve o **estado depois** da ação, e não o estrago que a
 * ação causa. A diferença importa num caso real: se o canal já estava ligado
 * sem credencial, nada saía por ele antes e nada sai depois — dizer que
 * desligá-lo "silenciou" a operação seria atribuir à pessoa um efeito que ela
 * não teve, e é o tipo de frase que faz alguém desconfiar do resto da tela.
 *
 * **Não há controle para `disponivel`, e não é omissão**: a credencial vive no
 * `.env` do servidor e mudá-la é deploy (ADR 0006). Um controle aqui prometeria
 * o que esta tela não consegue fazer.
 */
export function AcaoCanal({
  canal,
  silenciaTudo,
}: {
  canal: CanalOut;
  /**
   * Desligar este canal deixa a operação sem nenhum canal enviando? Calculado
   * pela página a partir da lista inteira — ver a docstring do componente.
   */
  silenciaTudo: boolean;
}) {
  const router = useRouter();
  const [estado, setEstado] = useState<Estado>({ tipo: "fechado" });
  const [enviando, setEnviando] = useState(false);
  const [recarregando, iniciarRecarga] = useTransition();

  const ocupado = enviando || recarregando;
  const nome = rotuloDoCanal(canal.canal);

  async function salvar(habilitado: boolean) {
    setEnviando(true);
    try {
      await atualizarCanal(API_BASE_URL, canal.canal, { habilitado });
      setEnviando(false);
      setEstado({ tipo: "fechado" });
      // A resposta traz o canal alterado, mas quem desenha o estado é o
      // servidor: é ele que sabe se o *outro* canal continua enviando, e é essa
      // conta que muda a força da próxima confirmação. Recarregar é também o
      // que faz aparecer, ao ligar um canal sem credencial, a frase que diz que
      // nada vai sair mesmo assim.
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
      // O controle **não** sai de cena no 403, ao contrário do que a fila de
      // pendências faz: aqui a recusa é quase sempre uma aba antiga de gestor
      // ou um papel alterado no servidor, e nos dois casos o estado que a tela
      // mostra continua válido para leitura — que é o que o gestor tem direito
      // de ver. Sumir com o botão trocaria uma frase explicativa por uma tela
      // que muda de forma sozinha.
      setEstado({ tipo: "erro", mensagem: mensagemDoErro(causa) });
    }
  }

  if (estado.tipo === "confirmando") {
    return (
      <div className="grid gap-2 rounded-xl border border-red-200 bg-red-50 p-3 sm:w-96">
        {silenciaTudo ? (
          <p className="m-0 text-xs leading-5 text-red-700">
            Desligar o <strong>{nome}</strong>? Depois disto{" "}
            <strong>nenhum canal envia</strong>: nenhum alerta — documento incompleto, prazo de
            competência, volume anormal, pendência parada — chega a ninguém, por caminho
            nenhum, até alguém ligar um canal que tenha credenciais no servidor.
          </p>
        ) : (
          <p className="m-0 text-xs leading-5 text-red-700">
            Desligar o <strong>{nome}</strong>? A partir daí nenhum aviso sai por ele.
          </p>
        )}
        <p className="m-0 text-xs leading-5 text-red-700">
          E ninguém é avisado de que parou:{" "}
          <strong>a ausência de alerta é indistinguível de &ldquo;não havia o que alertar&rdquo;</strong>{" "}
          — o mesmo que o log de alertas avisa sobre o cooldown.
        </p>
        <p className="m-0 text-xs leading-5 text-red-700">
          O que já saiu continua no log de alertas, esta mudança fica registrada no histórico
          abaixo, e religar aqui é um clique.
        </p>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="btn btn--primary h-9 min-h-9 px-3 text-xs"
            disabled={ocupado}
            onClick={() => void salvar(false)}
          >
            {ocupado ? "Desligando…" : "Confirmar desligamento"}
          </button>
          <button
            type="button"
            className="btn btn--secondary h-9 min-h-9 px-3 text-xs"
            disabled={ocupado}
            onClick={() => setEstado({ tipo: "fechado" })}
          >
            Cancelar
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="grid justify-items-start gap-2">
      {canal.habilitado ? (
        <button
          type="button"
          className="btn btn--secondary h-9 min-h-9 px-3 text-xs"
          disabled={ocupado}
          onClick={() => setEstado({ tipo: "confirmando" })}
        >
          {recarregando ? "Atualizando…" : `Desligar ${nome}`}
        </button>
      ) : (
        <button
          type="button"
          className="btn btn--primary h-9 min-h-9 px-3 text-xs"
          disabled={ocupado}
          onClick={() => void salvar(true)}
        >
          {ocupado ? "Ligando…" : `Ligar ${nome}`}
        </button>
      )}

      {/* A frase sai como a API a escreveu, minúscula inicial inclusive — é o
          que o resto do app faz com mensagem de erro da API, e é ela que diz o
          que fazer. */}
      {estado.tipo === "erro" && (
        <p role="alert" className="alert--error text-xs">
          {estado.mensagem}
        </p>
      )}
    </div>
  );
}
