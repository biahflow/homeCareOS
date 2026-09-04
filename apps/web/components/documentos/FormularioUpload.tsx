"use client";

import { uploadDocumento, ApiError } from "@homecareos/contracts";
import type { DocumentoCriado } from "@homecareos/contracts";
import { useRouter } from "next/navigation";
import { useId, useState, useTransition } from "react";
import { API_BASE_URL } from "@/lib/env";
import { ROTULO_DE_STATUS_DOCUMENTO, varianteDeStatus } from "./vocabulario";

type EstadoEnvio =
  | { tipo: "ocioso" }
  | { tipo: "enviando" }
  | { tipo: "sucesso"; documentos: DocumentoCriado[] }
  | { tipo: "erro"; mensagem: string };

/**
 * O envio da evolução escaneada.
 *
 * Client Component, e o único desta tela: a listagem abaixo é renderizada no
 * servidor. Um upload é `FormData` saindo do navegador com o arquivo que a
 * pessoa escolheu — não há como o servidor fazer isso por ela.
 *
 * Depois de um envio bem-sucedido a listagem precisa mudar, e quem a tem é o
 * servidor: `router.refresh()` a busca de novo com os filtros que já estão na
 * URL. Remendar a lista aqui no cliente exigiria inventar os campos que o
 * upload não devolve (`tipo`, `created_at`, operadora) e mostraria um documento
 * diferente do que a API tem.
 */
export function FormularioUpload() {
  const router = useRouter();
  const competenciaId = useId();
  const [arquivo, setArquivo] = useState<File | null>(null);
  // Uma chave por arquivo selecionado: reenviar o mesmo arquivo (retentativa)
  // reusa a chave; trocar o arquivo gera uma nova.
  const [idempotencyKey, setIdempotencyKey] = useState<string | null>(null);
  const [competencia, setCompetencia] = useState("");
  const [estado, setEstado] = useState<EstadoEnvio>({ tipo: "ocioso" });
  const [recarregando, iniciarRecarga] = useTransition();

  function handleArquivoChange(event: React.ChangeEvent<HTMLInputElement>) {
    const proximoArquivo = event.target.files?.[0] ?? null;
    setArquivo(proximoArquivo);
    setIdempotencyKey(proximoArquivo ? crypto.randomUUID() : null);
    setEstado({ tipo: "ocioso" });
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!arquivo || !idempotencyKey || !competencia) return;

    setEstado({ tipo: "enviando" });
    try {
      const resposta = await uploadDocumento(API_BASE_URL, {
        arquivo,
        competencia,
        idempotencyKey,
      });
      setEstado({ tipo: "sucesso", documentos: resposta.documentos });
      // Dentro da transição para o botão continuar desabilitado até a listagem
      // voltar do servidor: reabilitá-lo antes convida ao segundo clique, que
      // com a mesma `Idempotency-Key` não cria nada e com uma nova duplicaria o
      // documento.
      iniciarRecarga(() => {
        router.refresh();
      });
    } catch (erro) {
      if (erro instanceof ApiError && erro.status === 401) {
        // A sessão acabou no servidor: ela expirou, foi revogada no logout, ou
        // um login novo no mesmo navegador tomou o lugar dela
        // (`sessoes.revogar(token_anterior)`, em `auth/router.py`). A aba antiga
        // não tem como se recuperar sozinha — em vez de repetir "credencial
        // inválida" a cada tentativa, ela volta para o login dizendo o que houve.
        router.replace("/login?motivo=sessao-encerrada");
        router.refresh();
        return;
      }
      const mensagem =
        erro instanceof ApiError
          ? erro.message
          : "Falha inesperada ao enviar o documento. Tente novamente.";
      setEstado({ tipo: "erro", mensagem });
    }
  }

  const enviando = estado.tipo === "enviando" || recarregando;
  const podeEnviar = Boolean(arquivo && competencia) && !enviando;

  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>Enviar documento</h2>
      </div>

      <form className="form-grid" onSubmit={handleSubmit}>
        <div className="grid gap-1.5">
          <label htmlFor="arquivo" className="form-label">
            Arquivo (PDF, JPG ou PNG)
          </label>
          <input
            id="arquivo"
            name="arquivo"
            type="file"
            accept="application/pdf,image/jpeg,image/png"
            onChange={handleArquivoChange}
            required
            className="field"
          />
        </div>

        <div className="grid max-w-xs gap-1.5">
          <label htmlFor={competenciaId} className="form-label">
            Competência
          </label>
          <input
            id={competenciaId}
            name="competencia"
            type="month"
            value={competencia}
            onChange={(event) => setCompetencia(event.target.value)}
            required
            className="field"
          />
          <p className="text-xs text-muted">
            Não é extraível do documento — informe manualmente (AAAA-MM).
          </p>
        </div>

        <button type="submit" className="btn btn--primary w-fit" disabled={!podeEnviar}>
          {enviando ? "Enviando…" : "Enviar"}
        </button>
      </form>

      {estado.tipo === "enviando" && (
        <p className="empty-state mt-4">Enviando e processando o arquivo…</p>
      )}

      {estado.tipo === "erro" && (
        <p role="alert" className="alert--error mt-4">
          {estado.mensagem}
        </p>
      )}

      {estado.tipo === "sucesso" && (
        <div className="mt-4 grid gap-2 rounded-xl border border-line bg-canvas p-4">
          {/* Um PDF de dez páginas vira dez documentos, e cada um é conferido
              sozinho. Dizer quantos nasceram deste envio evita a leitura de que
              "o documento" foi enviado uma vez só. */}
          <p role="status" className="m-0 text-sm font-semibold text-ink">
            {estado.documentos.length}{" "}
            {estado.documentos.length === 1
              ? "documento criado neste envio"
              : "documentos criados neste envio"}
          </p>
          <ul className="m-0 grid list-none gap-1 p-0">
            {estado.documentos.map((documento) => (
              <li
                key={documento.id}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted"
              >
                <span>
                  Página {documento.pagina} · competência {documento.competencia}
                </span>
                <span className={`state ${varianteDeStatus(documento.status)}`}>
                  {ROTULO_DE_STATUS_DOCUMENTO[documento.status]}
                </span>
              </li>
            ))}
          </ul>
          {/* A listagem já foi recarregada, mas ela é filtrada e paginada: um
              documento novo pode legitimamente não aparecer nela. Sem esta
              frase, a ausência parece falha do envio. */}
          <p className="m-0 text-xs text-muted">
            A listagem abaixo foi atualizada. Se algum destes não aparecer nela, é o filtro ou a
            página em vigor — o documento foi criado.
          </p>
        </div>
      )}
    </section>
  );
}
