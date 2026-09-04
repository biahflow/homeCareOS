"use client";

import { uploadDocumento, ApiError } from "@homecareos/contracts";
import type { DocumentoCriado, DocumentoStatus } from "@homecareos/contracts";
import { useRouter } from "next/navigation";
import { useId, useState } from "react";
import { API_BASE_URL } from "@/lib/env";

type EstadoEnvio =
  | { tipo: "ocioso" }
  | { tipo: "enviando" }
  | { tipo: "sucesso"; documentos: DocumentoCriado[] }
  | { tipo: "erro"; mensagem: string };

const STATUS_VARIANTE: Record<DocumentoStatus, string> = {
  processando: "state--0",
  aprovado: "state--1",
  resolvido: "state--1",
  liberado: "state--1",
  incompleto: "state--2",
  em_correcao: "state--2",
  problema: "state--3",
};

const STATUS_LABEL: Record<DocumentoStatus, string> = {
  processando: "Processando",
  aprovado: "Aprovado",
  problema: "Problema",
  incompleto: "Incompleto",
  em_correcao: "Em correção",
  resolvido: "Resolvido",
  liberado: "Liberado",
};

export default function DocumentosPage() {
  const router = useRouter();
  const competenciaId = useId();
  const [arquivo, setArquivo] = useState<File | null>(null);
  // Uma chave por arquivo selecionado: reenviar o mesmo arquivo (retentativa)
  // reusa a chave; trocar o arquivo gera uma nova.
  const [idempotencyKey, setIdempotencyKey] = useState<string | null>(null);
  const [competencia, setCompetencia] = useState("");
  const [estado, setEstado] = useState<EstadoEnvio>({ tipo: "ocioso" });

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

  const enviando = estado.tipo === "enviando";
  const podeEnviar = Boolean(arquivo && competencia) && !enviando;

  return (
    <div className="grid gap-6">
      <div className="page-head">
        <p className="eyebrow">Operação</p>
        <h1>Documentos</h1>
        <p>
          Envie a evolução de prontuário escaneada (PDF ou imagem) para conferência antes do envio
          à operadora.
        </p>
      </div>

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
      </section>

      <section className="panel">
        <div className="panel-heading">
          <h2>Documentos desta sessão</h2>
          {estado.tipo === "sucesso" && (
            <span className="state state--1">
              {estado.documentos.length}{" "}
              {estado.documentos.length === 1 ? "documento criado" : "documentos criados"}
            </span>
          )}
        </div>

        {estado.tipo === "ocioso" && (
          <p className="empty-state">
            Nenhum documento enviado ainda nesta sessão. O histórico de documentos enviados
            anteriormente ainda não está disponível aqui — depende de{" "}
            <code>GET /api/documentos</code> (issue #6).
          </p>
        )}

        {estado.tipo === "enviando" && (
          <p className="empty-state">Enviando e processando o arquivo…</p>
        )}

        {estado.tipo === "erro" && (
          <p role="alert" className="alert--error">
            {estado.mensagem}
          </p>
        )}

        {estado.tipo === "sucesso" && (
          <div className="panel-rows">
            {estado.documentos.map((documento) => (
              <div key={documento.id} className="row">
                <span>
                  Página {documento.pagina} · competência {documento.competencia}
                </span>
                <span className={`state ${STATUS_VARIANTE[documento.status]}`}>
                  {STATUS_LABEL[documento.status]}
                </span>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
