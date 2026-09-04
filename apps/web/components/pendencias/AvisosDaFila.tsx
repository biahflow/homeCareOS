"use client";

import { X } from "lucide-react";
import { createContext, useContext, useState } from "react";

/**
 * O lugar onde a fila avisa que **mudou debaixo de quem estava olhando**.
 *
 * Ele existe fora da linha da pendência por um motivo concreto: quando duas
 * conferentes disputam o mesmo item, a resposta ao 422 é recarregar a lista — e
 * a linha que recebeu o erro pode sair da página no recarregamento (ela mudou
 * de status e há filtro de status em vigor). Uma mensagem presa àquela linha
 * desapareceria junto, deixando a pessoa com uma lista que mudou sozinha e
 * nenhuma explicação. Aqui, ela sobrevive ao `router.refresh()`.
 *
 * O provider é Client Component, mas a lista continua sendo renderizada no
 * servidor: ela chega como `children`, e `children` de um Client Component não
 * é convertido em cliente.
 */
const ContextoDeAvisos = createContext<((mensagem: string | null) => void) | null>(null);

/** Publica um aviso na área acima da lista. `null` limpa o que estiver lá. */
export function useAvisoDaFila(): (mensagem: string | null) => void {
  const definir = useContext(ContextoDeAvisos);
  if (definir === null) {
    throw new Error("useAvisoDaFila precisa estar dentro de <AvisosDaFila>.");
  }
  return definir;
}

export function AvisosDaFila({ children }: { children: React.ReactNode }) {
  const [aviso, setAviso] = useState<string | null>(null);

  return (
    <ContextoDeAvisos.Provider value={setAviso}>
      {aviso !== null && (
        // `role="status"` e não `alert`: é informação sobre o estado da fila,
        // não um erro da pessoa — o leitor de tela anuncia sem interromper.
        <p role="status" className="alert--info mb-4 items-start justify-between">
          <span>{aviso}</span>
          <button
            type="button"
            onClick={() => setAviso(null)}
            aria-label="Dispensar aviso"
            className="shrink-0 text-muted hover:text-ink"
          >
            <X size={14} />
          </button>
        </p>
      )}
      {children}
    </ContextoDeAvisos.Provider>
  );
}
