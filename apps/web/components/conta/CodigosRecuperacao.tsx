"use client";

import { Check, Copy, Download } from "lucide-react";
import { useId, useState } from "react";

const NOME_DO_ARQUIVO = "homecareos-codigos-recuperacao.txt";

/**
 * A exibição **única** dos códigos de recuperação.
 *
 * `POST /api/auth/mfa/confirmar` é o único lugar em que eles existem em claro:
 * o banco guarda apenas o hash Argon2id, não há endpoint que os mostre de novo,
 * e quem os perder junto com o celular vai precisar de alguém com acesso ao
 * banco. Tudo nesta tela sai daí:
 *
 * - **copiar e baixar**, porque transcrever oito códigos à mão de uma tela para
 *   um papel erra — e o erro só aparece no dia em que o celular sumiu;
 * - **confirmação explícita para sair**, porque um clique acidental em
 *   "concluir" custa os oito códigos de uma vez;
 * - **nada persistido no cliente**: os códigos vivem no estado deste
 *   componente e acabam com ele. Guardá-los no armazenamento do navegador para
 *   "facilitar depois" desfaria o motivo de a API só guardar o hash — e a
 *   conferência acontece em estação compartilhada, onde o próximo turno usa o
 *   mesmo navegador.
 *
 * Por isso também não há como voltar para cá: a saída é de mão única, e o
 * componente pai descarta o array ao trocar de passo.
 */
export function CodigosRecuperacao({
  codigos,
  onConcluir,
}: {
  codigos: string[];
  onConcluir: () => void;
}) {
  const confirmacaoId = useId();
  const [confirmado, setConfirmado] = useState(false);
  const [copia, setCopia] = useState<"ocioso" | "copiado" | "falhou">("ocioso");

  async function handleCopiar() {
    try {
      // Só os códigos, um por linha: é o formato que se cola direto num campo
      // de anotação de gerenciador de senhas, sem sobrar cabeçalho para limpar.
      await navigator.clipboard.writeText(codigos.join("\n"));
      setCopia("copiado");
    } catch {
      // Área de transferência bloqueada (permissão negada, contexto não
      // seguro). Dizer que copiou sem ter copiado é o pior desfecho possível
      // aqui: a pessoa fecharia a tela confiando numa cópia que não existe.
      setCopia("falhou");
    }
  }

  function handleBaixar() {
    const conteudo = montarArquivo(codigos);
    const blob = new Blob([conteudo], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);

    const link = document.createElement("a");
    link.href = url;
    link.download = NOME_DO_ARQUIVO;
    document.body.append(link);
    link.click();
    link.remove();

    // Revogar não é higiene opcional: enquanto a URL `blob:` viver, os códigos
    // continuam acessíveis por um endereço dentro deste documento. A volta pelo
    // event loop existe porque o download já foi disparado pelo clique, mas
    // parte dos navegadores ainda está lendo o blob quando esta linha roda.
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>Guarde os códigos de recuperação</h2>
        <span className="state state--1">Segundo fator ativado</span>
      </div>

      <div className="grid gap-4">
        {/* O `<span>` não é enfeite: `.alert--error` é um flex container, e um
            `<strong>` solto viraria uma coluna à parte no meio da frase. */}
        <p role="alert" className="alert--error">
          <span>
            <strong>Esta é a única vez que estes códigos aparecem.</strong> Não há como pedi-los de
            novo: o sistema guarda apenas uma versão embaralhada deles. Se você perder os códigos e
            o celular, só recupera o acesso com quem administra o banco de dados.
          </span>
        </p>

        <p className="text-sm leading-6 text-muted">
          Cada código vale <strong className="text-ink">uma única vez</strong> e substitui os seis
          dígitos do aplicativo autenticador na tela de login — é o caminho para quando o celular
          não estiver à mão.
        </p>

        <ul className="grid grid-cols-2 gap-2 rounded-xl border border-line bg-canvas p-4 sm:grid-cols-4">
          {codigos.map((codigo) => (
            <li key={codigo} className="text-center font-mono text-sm tracking-wide text-ink">
              {codigo}
            </li>
          ))}
        </ul>

        <div className="flex flex-wrap items-center gap-2">
          <button type="button" onClick={handleCopiar} className="btn btn--secondary">
            {copia === "copiado" ? <Check size={16} /> : <Copy size={16} />}
            {copia === "copiado" ? "Copiado" : "Copiar códigos"}
          </button>
          <button type="button" onClick={handleBaixar} className="btn btn--secondary">
            <Download size={16} />
            Baixar .txt
          </button>
        </div>

        {copia === "falhou" && (
          <p role="alert" className="alert--error">
            Não foi possível copiar automaticamente. Baixe o arquivo, ou selecione os códigos acima
            e copie manualmente.
          </p>
        )}

        <p className="alert--info">
          A estação é compartilhada: se você baixou o arquivo, tire-o da pasta de downloads e da
          área de trabalho antes de encerrar o turno.
        </p>

        <label htmlFor={confirmacaoId} className="flex items-start gap-2.5 text-sm text-ink">
          <input
            id={confirmacaoId}
            type="checkbox"
            checked={confirmado}
            onChange={(event) => setConfirmado(event.target.checked)}
            className="mt-0.5 size-4 accent-brand-500"
          />
          Guardei os códigos em um lugar seguro.
        </label>

        <button
          type="button"
          onClick={onConcluir}
          disabled={!confirmado}
          className="btn btn--primary w-fit"
        >
          Concluir
        </button>
      </div>
    </section>
  );
}

/**
 * O `.txt` que a pessoa baixa. Leva o cabeçalho que o arquivo vai precisar
 * daqui a seis meses, quando ele for encontrado sozinho numa pasta: o que ele
 * é, para que serve e que cada código morre no primeiro uso.
 */
function montarArquivo(codigos: string[]): string {
  const gerado = new Date().toLocaleString("pt-BR");
  return [
    "Códigos de recuperação — HomeCareOS",
    `Gerados em ${gerado}`,
    "",
    "Cada código vale UMA vez e substitui o código do aplicativo autenticador",
    "na tela de login. Eles não podem ser exibidos de novo.",
    "",
    ...codigos,
    "",
  ].join("\n");
}
