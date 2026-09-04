"use client";

import { useRouter } from "next/navigation";
import { useId, useState, useTransition } from "react";
import { ApiError, detalhesDeValidacao, registrarBaseline } from "@homecareos/contracts";
import type { BaselineOut, BaselineUpsert, Operadora } from "@homecareos/contracts";
import { API_BASE_URL } from "@/lib/env";
import { centavosDeReais, formatarCompetencia, formatarReais } from "./formatos";

/**
 * O cadastro do baseline de glosa — **a única ação de escrita desta tela**.
 *
 * A autorização aqui é o **inverso** da fila de pendências: lá o gestor é quem
 * não transiciona; aqui ele é o único que escreve
 * (`dependencies=[Depends(exigir_papel(Papel.GESTOR))]` no `PUT`). O baseline é
 * a régua contra a qual o próprio sistema é medido, e quem opera a conferência
 * não mexe na régua que a mede (ADR 0001). Este componente só é renderizado
 * para o gestor, o que é ergonomia; a autoridade continua sendo o 403 da API,
 * tratado abaixo.
 *
 * O `PUT` é **upsert** pela chave natural `(competencia, operadora_id)`: gravar
 * de novo substitui os valores, sem aviso da API. Por isso o formulário avisa
 * antes, quando já existe baseline para a combinação escolhida — e por isso não
 * há exclusão: a API não tem `DELETE`, e corrigir é gravar de novo.
 */

const MENSAGEM_SEM_PERMISSAO =
  "Seu papel não permite registrar baseline: ele é dado de gestão e só o gestor o escreve. Nada foi alterado.";

const MENSAGEM_INESPERADA = "Falha inesperada ao registrar o baseline. Tente novamente.";

/** Chave natural do baseline, para achar o que já existe. `""` é o consolidado. */
function chaveDoBaseline(competencia: string, operadoraId: string): string {
  return `${competencia}|${operadoraId}`;
}

type Estado =
  | { tipo: "ocioso" }
  | { tipo: "erro"; mensagens: string[] }
  | { tipo: "negado"; mensagem: string }
  | { tipo: "sucesso"; baseline: BaselineOut };

/** Inteiro não negativo digitado num campo obrigatório. */
function lerContagem(texto: string, campo: string): { valor: number } | { erro: string } {
  const limpo = texto.trim();
  if (limpo === "") {
    return { erro: `Informe ${campo}.` };
  }
  const valor = Number(limpo);
  if (!Number.isSafeInteger(valor) || valor < 0) {
    return { erro: `${campo} precisa ser um número inteiro não negativo.` };
  }
  return { valor };
}

/** Horas com casa decimal, opcional. Aceita vírgula, que é como se digita em pt-BR. */
function lerHoras(texto: string): { valor: number | null } | { erro: string } {
  const limpo = texto.trim().replace(",", ".");
  if (limpo === "") {
    return { valor: null };
  }
  const valor = Number(limpo);
  if (!Number.isFinite(valor) || valor < 0) {
    return { erro: "Horas de conferência precisa ser um número não negativo." };
  }
  return { valor };
}

export function FormularioBaseline({
  operadoras,
  baselines,
}: {
  operadoras: Operadora[];
  /** Os já registrados, para avisar antes de sobrescrever um deles. */
  baselines: BaselineOut[];
}) {
  const router = useRouter();
  const [enviando, setEnviando] = useState(false);
  const [recarregando, iniciarRecarga] = useTransition();
  const [estado, setEstado] = useState<Estado>({ tipo: "ocioso" });

  const [competencia, setCompetencia] = useState("");
  const [operadoraId, setOperadoraId] = useState("");
  const [enviados, setEnviados] = useState("");
  const [glosados, setGlosados] = useState("");
  const [valor, setValor] = useState("");
  const [horas, setHoras] = useState("");
  const [fonte, setFonte] = useState("");
  const [observacao, setObservacao] = useState("");

  const competenciaCampo = useId();
  const operadoraCampo = useId();
  const enviadosCampo = useId();
  const glosadosCampo = useId();
  const valorCampo = useId();
  const horasCampo = useId();
  const fonteCampo = useId();
  const observacaoCampo = useId();

  const ocupado = enviando || recarregando;

  // O que a pessoa digitou no campo de dinheiro, já convertido — é o que
  // aparece de volta abaixo do campo, antes de qualquer envio. Reais entram,
  // centavos inteiros saem, e a conta é visível: é o ponto onde um erro de 100x
  // seria cometido em silêncio.
  const leituraDoValor = centavosDeReais(valor);

  const jaRegistrado = baselines.find(
    (baseline) =>
      chaveDoBaseline(baseline.competencia, baseline.operadora_id ?? "") ===
      chaveDoBaseline(competencia, operadoraId),
  );

  function montarCorpo(): { corpo: BaselineUpsert } | { erros: string[] } {
    const erros: string[] = [];

    if (competencia === "") {
      erros.push("Informe a competência (AAAA-MM).");
    }

    const leituraEnviados = lerContagem(enviados, "documentos enviados");
    const leituraGlosados = lerContagem(glosados, "documentos glosados");
    if ("erro" in leituraEnviados) erros.push(leituraEnviados.erro);
    if ("erro" in leituraGlosados) erros.push(leituraGlosados.erro);

    // A regra é da API (`BaselineUpsert._glosados_nao_passam_de_enviados`, com o
    // `CheckConstraint` do banco como rede de segurança). Repeti-la aqui é só
    // para poupar uma ida ao servidor numa comparação trivial — quando o 422
    // chega mesmo assim, quem fala é a mensagem dela, logo abaixo.
    if ("valor" in leituraEnviados && "valor" in leituraGlosados) {
      if (leituraGlosados.valor > leituraEnviados.valor) {
        erros.push(
          "Documentos glosados não pode ser maior que documentos enviados: a operadora não recusa mais do que recebeu.",
        );
      }
    }

    if (leituraDoValor.tipo === "invalido") {
      erros.push(leituraDoValor.motivo);
    }

    const leituraHoras = lerHoras(horas);
    if ("erro" in leituraHoras) erros.push(leituraHoras.erro);

    if (fonte.trim() === "") {
      erros.push("Informe a fonte: de qual demonstrativo ou relatório estes números vieram.");
    }

    if (
      erros.length > 0 ||
      !("valor" in leituraEnviados) ||
      !("valor" in leituraGlosados) ||
      !("valor" in leituraHoras)
    ) {
      // O `||` acima é o que o TypeScript precisa para estreitar os três tipos;
      // toda falha real já empurrou a sua mensagem, e a lista nunca sai vazia
      // por este caminho. O fallback existe para que, se um dia saísse, o botão
      // não ficasse mudo.
      return { erros: erros.length > 0 ? erros : [MENSAGEM_INESPERADA] };
    }

    return {
      corpo: {
        competencia,
        // String vazia é **consolidado de todas as operadoras**, e vira `null`
        // no corpo. Mandar `""` seria um UUID inválido; omitir a intenção seria
        // pior: são linhas diferentes no banco.
        operadora_id: operadoraId === "" ? null : operadoraId,
        documentos_enviados: leituraEnviados.valor,
        documentos_glosados: leituraGlosados.valor,
        valor_glosado_centavos:
          leituraDoValor.tipo === "ok" ? leituraDoValor.centavos : null,
        horas_conferencia: leituraHoras.valor,
        fonte: fonte.trim(),
        observacao: observacao.trim() === "" ? null : observacao.trim(),
      },
    };
  }

  async function enviar(evento: React.FormEvent<HTMLFormElement>) {
    evento.preventDefault();
    setEstado({ tipo: "ocioso" });

    const montado = montarCorpo();
    if ("erros" in montado) {
      setEstado({ tipo: "erro", mensagens: montado.erros });
      return;
    }

    setEnviando(true);
    try {
      const baseline = await registrarBaseline(API_BASE_URL, montado.corpo);
      setEnviando(false);
      setEstado({ tipo: "sucesso", baseline });
      // As métricas do topo passam a exibir o bloco de glosa informada desta
      // competência; quem tem o retrato certo é o servidor.
      iniciarRecarga(() => {
        router.refresh();
      });
    } catch (causa) {
      if (causa instanceof ApiError && causa.status === 401) {
        // A sessão acabou no servidor — mesmo tratamento do resto da área
        // logada. `enviando` continua ligado de propósito: a navegação já está
        // em curso.
        router.replace("/login?motivo=sessao-encerrada");
        router.refresh();
        return;
      }

      setEnviando(false);

      if (causa instanceof ApiError && causa.status === 403) {
        // Insistir não muda nada: a recusa é do papel, não do momento.
        setEstado({ tipo: "negado", mensagem: MENSAGEM_SEM_PERMISSAO });
        return;
      }
      if (causa instanceof ApiError) {
        // A regra recusada é da API: mostramos o que ela escreveu. O 422 de
        // validação de corpo esconde a frase útil em `detalhes` — `message`
        // sozinha diria só "parâmetros inválidos".
        const detalhes = detalhesDeValidacao(causa);
        setEstado({
          tipo: "erro",
          mensagens: detalhes.length > 0 ? detalhes : [causa.message],
        });
        return;
      }
      setEstado({ tipo: "erro", mensagens: [MENSAGEM_INESPERADA] });
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
    <form className="form-grid" onSubmit={enviar}>
      <p className="alert--info">
        Estes números vêm do <strong>demonstrativo da operadora</strong>, não do sistema: é a glosa
        que aconteceu depois do envio. Gravar de novo a mesma competência e operadora{" "}
        <strong>substitui</strong> os valores registrados — a API não tem exclusão.
      </p>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="grid gap-1.5">
          <label htmlFor={competenciaCampo} className="form-label">
            Competência
          </label>
          <input
            id={competenciaCampo}
            type="month"
            className="field"
            value={competencia}
            onChange={(evento) => setCompetencia(evento.target.value)}
            disabled={ocupado}
            required
          />
        </div>

        <div className="grid gap-1.5">
          <label htmlFor={operadoraCampo} className="form-label">
            Operadora
          </label>
          <select
            id={operadoraCampo}
            className="field"
            value={operadoraId}
            onChange={(evento) => setOperadoraId(evento.target.value)}
            disabled={ocupado}
          >
            <option value="">Consolidado (todas as operadoras)</option>
            {operadoras.map((operadora) => (
              <option key={operadora.id} value={operadora.id}>
                {operadora.nome}
              </option>
            ))}
          </select>
          <p className="text-xs text-muted">
            Sem operadora, o registro é o <strong>consolidado de todas</strong> — é ele que a
            métrica usa quando nenhuma operadora está filtrada. Não é “operadora desconhecida”.
          </p>
        </div>

        <div className="grid gap-1.5">
          <label htmlFor={enviadosCampo} className="form-label">
            Documentos enviados
          </label>
          <input
            id={enviadosCampo}
            type="number"
            min={0}
            step={1}
            className="field"
            value={enviados}
            onChange={(evento) => setEnviados(evento.target.value)}
            disabled={ocupado}
            required
          />
        </div>

        <div className="grid gap-1.5">
          <label htmlFor={glosadosCampo} className="form-label">
            Documentos glosados
          </label>
          <input
            id={glosadosCampo}
            type="number"
            min={0}
            step={1}
            className="field"
            value={glosados}
            onChange={(evento) => setGlosados(evento.target.value)}
            disabled={ocupado}
            required
          />
          <p className="text-xs text-muted">Nunca maior que os enviados.</p>
        </div>

        <div className="grid gap-1.5">
          <label htmlFor={valorCampo} className="form-label">
            Valor glosado (R$) — opcional
          </label>
          <input
            id={valorCampo}
            type="text"
            inputMode="decimal"
            placeholder="1.234,56"
            className="field"
            value={valor}
            onChange={(evento) => setValor(evento.target.value)}
            disabled={ocupado}
            aria-describedby={`${valorCampo}-ajuda`}
          />
          <p id={`${valorCampo}-ajuda`} className="text-xs text-muted">
            {/* O valor convertido aparece antes do envio: é o que impede o erro
                de 100x passar despercebido entre reais e centavos. */}
            {leituraDoValor.tipo === "ok" ? (
              <>
                Será registrado: <strong className="text-ink">{formatarReais(leituraDoValor.centavos)}</strong>
              </>
            ) : leituraDoValor.tipo === "invalido" ? (
              <span className="text-danger">{leituraDoValor.motivo}</span>
            ) : (
              "Digite em reais, com vírgula nos centavos. Em branco significa “não informado”."
            )}
          </p>
        </div>

        <div className="grid gap-1.5">
          <label htmlFor={horasCampo} className="form-label">
            Horas de conferência — opcional
          </label>
          <input
            id={horasCampo}
            type="text"
            inputMode="decimal"
            placeholder="40,5"
            className="field"
            value={horas}
            onChange={(evento) => setHoras(evento.target.value)}
            disabled={ocupado}
          />
        </div>
      </div>

      <div className="grid gap-1.5">
        <label htmlFor={fonteCampo} className="form-label">
          Fonte
        </label>
        <input
          id={fonteCampo}
          type="text"
          className="field"
          placeholder="Demonstrativo de glosa da operadora, recebido em 10/09"
          value={fonte}
          onChange={(evento) => setFonte(evento.target.value)}
          disabled={ocupado}
          required
        />
        <p className="text-xs text-muted">
          Obrigatória: é ela que aparece ao lado do número, para quem lê o painel saber de onde ele
          veio.
        </p>
      </div>

      <div className="grid gap-1.5">
        <label htmlFor={observacaoCampo} className="form-label">
          Observação — opcional
        </label>
        <textarea
          id={observacaoCampo}
          className="field"
          rows={2}
          value={observacao}
          onChange={(evento) => setObservacao(evento.target.value)}
          disabled={ocupado}
        />
      </div>

      {jaRegistrado !== undefined && (
        <p role="status" className="alert--info">
          Já existe baseline para{" "}
          <strong className="text-ink">{formatarCompetencia(jaRegistrado.competencia)}</strong>
          {jaRegistrado.operadora_id === null ? " (consolidado)" : ""}: {jaRegistrado.documentos_glosados}{" "}
          glosados de {jaRegistrado.documentos_enviados} enviados, fonte “{jaRegistrado.fonte}”.
          Gravar substitui esses valores.
        </p>
      )}

      {estado.tipo === "erro" && (
        <div role="alert" className="alert--error flex-col gap-1">
          {estado.mensagens.map((mensagem, indice) => (
            <span key={`${indice}-${mensagem}`}>{mensagem}</span>
          ))}
        </div>
      )}

      {estado.tipo === "sucesso" && (
        <p role="status" className="alert--info">
          Baseline de{" "}
          <strong className="text-ink">{formatarCompetencia(estado.baseline.competencia)}</strong>{" "}
          registrado: {estado.baseline.documentos_glosados} glosados de{" "}
          {estado.baseline.documentos_enviados} enviados
          {estado.baseline.valor_glosado_centavos !== null && (
            <>
              , {/* Vem da resposta da API: é o valor que de fato foi gravado. */}
              <strong className="text-ink">
                {formatarReais(estado.baseline.valor_glosado_centavos)}
              </strong>
            </>
          )}
          . Fonte: {estado.baseline.fonte}.
        </p>
      )}

      <button type="submit" className="btn btn--primary w-fit" disabled={ocupado}>
        {enviando ? "Registrando…" : "Registrar baseline"}
      </button>
    </form>
  );
}
