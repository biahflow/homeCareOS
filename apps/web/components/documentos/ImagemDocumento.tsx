"use client";

import { useState } from "react";
import { caminhoArquivoDocumento } from "@homecareos/contracts";
import { API_BASE_URL } from "@/lib/env";

/**
 * Texto alternativo da página escaneada.
 *
 * **Descreve o que a imagem é, não quem ela é sobre.** Nenhum nome nem id de
 * paciente entra aqui: `alt` vai para o leitor de tela, para a aba do
 * navegador quando a imagem falha, e para qualquer ferramenta que copie o
 * HTML da tela — todos os três lugares errados para um dado de prontuário.
 */
const ALT_DA_PAGINA = "Página escaneada da evolução";

const MENSAGEM_SUMICO =
  "A página escaneada não está disponível. O arquivo pode ter sido removido do storage — a conferência continua possível com a extração abaixo, mas sem o documento ao lado.";

type Estado = "carregando" | "ok" | "erro";

/**
 * A página escaneada do documento, buscada em `GET /api/documentos/{id}/arquivo`
 * (PR #54).
 *
 * **Por que Client Component.** O 404 de "arquivo sumiu do storage" (documento
 * existe, chave não existe mais no bucket) não pode virar ícone de imagem
 * quebrada — é informação que quem confere precisa ler, não adivinhar de um
 * ícone genérico do navegador. Isso só é detectável em runtime, no
 * `onError` do `<img>`: uma verificação prévia no servidor (`HEAD` antes de
 * renderizar) checaria a existência num instante e ficaria obsoleta no
 * seguinte, e ainda pagaria uma segunda viagem de rede para o mesmo arquivo
 * que o `<img>` já vai buscar. `onError` cobre o mesmo defeito sem essa
 * duplicação, e cobre de brinde qualquer outra falha de carregamento (sessão
 * caída no meio, storage fora do ar) com a mesma mensagem — nenhuma delas
 * distingue causa aqui, porque a resposta para quem confere é a mesma:
 * seguir sem a imagem.
 *
 * **`<img>` simples, não `next/image`.** `next/image` reamostra a imagem para
 * os tamanhos que ele decide servir, e o que se conferre aqui é um documento
 * de prontuário — carimbo do COREN, assinatura, letra manuscrita. Reamostrar
 * pode borrar exatamente o traço que a pessoa abriu a tela para checar.
 * `next/image` também exige dimensões conhecidas de antemão ou domínio remoto
 * configurado; nenhum dos dois cabe aqui, onde a imagem vem do proxy da
 * própria origem em tamanho variável (uma página de PDF renderizada, ou uma
 * foto). Isto é decisão deliberada — não "esquecimento de otimizar": se for
 * revisitar, meça primeiro se o carimbo continua legível depois.
 */
export function ImagemDocumento({ documentoId }: { documentoId: string }) {
  const [estado, setEstado] = useState<Estado>("carregando");
  // Caminho relativo (API_BASE_URL é "" no navegador, ADR 0002): a requisição
  // sai para a própria origem do Next, `apps/web/proxy.ts` repassa para a API,
  // e o cookie de sessão viaja porque é a mesma origem — nunca `API_URL`, que
  // é variável de servidor e não existe no navegador.
  const caminho = caminhoArquivoDocumento(API_BASE_URL, documentoId);

  if (estado === "erro") {
    return <p className="empty-state">{MENSAGEM_SUMICO}</p>;
  }

  return (
    <div className="grid gap-3">
      <div className="overflow-hidden rounded-xl border border-line bg-canvas">
        {/* eslint-disable-next-line @next/next/no-img-element -- ver docstring
            do componente: next/image reamostra, e não pode borrar carimbo ou
            assinatura num documento de conferência. */}
        <img
          src={caminho}
          alt={ALT_DA_PAGINA}
          className="block max-h-[75vh] w-full object-contain"
          onLoad={() => setEstado("ok")}
          onError={() => setEstado("erro")}
        />
      </div>

      {/* `inline` no endpoint (PR #54) faz o navegador exibir em vez de
          baixar: abre a mesma página, só que em tamanho real, para quem
          quiser ampliar o que o card acima mostra reduzido. */}
      <a href={caminho} target="_blank" rel="noopener noreferrer" className="btn btn--secondary w-fit">
        Abrir em nova aba
      </a>
    </div>
  );
}
