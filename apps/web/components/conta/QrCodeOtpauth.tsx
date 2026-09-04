"use client";

import { useMemo } from "react";
import qrcode from "qrcode-generator";

/**
 * O QR code do cadastro do segundo fator, desenhado como **SVG inline**.
 *
 * Três decisões que não são estilo:
 *
 * - **SVG, e não `<img src="data:...">` de canvas.** A URI `otpauth://` é a
 *   credencial. Serializá-la numa imagem criaria uma segunda cópia dela — uma
 *   string longa num atributo do DOM, que vaza para qualquer ferramenta que
 *   copie HTML e que o navegador pode manter em cache de imagem. O SVG desenha
 *   a matriz de módulos: o que fica no DOM são retângulos, não a credencial.
 * - **`qrcode-generator`, com zero dependências transitivas.** Este código está
 *   no caminho de uma credencial; `qrcode` arrastaria `yargs`/`pngjs` e
 *   `react-qr-code` traria `prop-types`. Cada dependência aqui é superfície de
 *   supply chain sobre um segredo TOTP.
 * - **`aria-label` sem o segredo.** Quem usa leitor de tela não consegue
 *   escanear um QR code, e ouvir 32 caracteres em base32 não substitui isso —
 *   o caminho para essa pessoa é o segredo em texto, ao lado, que o leitor lê
 *   como texto de verdade. O rótulo aqui diz o que a imagem é; ler a credencial
 *   em voz alta numa estação compartilhada seria o contrário de ajudar.
 */
export function QrCodeOtpauth({ uri }: { uri: string }) {
  // Memoizado pela URI: o campo do código ao lado re-renderiza a cada tecla, e
  // a matriz do QR não muda entre uma tecla e outra.
  const { lado, caminho } = useMemo(() => desenhar(uri), [uri]);

  return (
    <svg
      viewBox={`0 0 ${lado} ${lado}`}
      role="img"
      aria-label="QR code para cadastrar o segundo fator no aplicativo autenticador."
      // Sem isto o navegador antialiasa a borda de cada módulo e a câmera lê um
      // quadriculado borrado nos tamanhos menores.
      shapeRendering="crispEdges"
      className="w-full max-w-60 rounded-lg border border-line bg-white"
    >
      {/* O fundo claro é parte do QR code, não decoração: a leitura depende do
          contraste entre módulo escuro e claro, e um SVG transparente sobre
          fundo colorido deixa de ser escaneável. */}
      <rect width={lado} height={lado} fill="#ffffff" />
      <path d={caminho} fill="#12110f" />
    </svg>
  );
}

/**
 * Zona silenciosa, em módulos. Quatro é o mínimo do padrão ISO/IEC 18004 — sem
 * ela, um leitor não acha as bordas do símbolo quando o QR encosta em outro
 * elemento da tela.
 */
const MARGEM = 4;

/** Um `<path>` só para a matriz inteira, em vez de um `<rect>` por módulo. */
function desenhar(uri: string): { lado: number; caminho: string } {
  // `0` é a versão automática: a menor que couber a URI. `M` (~15% de correção
  // de erro) é o nível que todo app autenticador assume neste tipo de código.
  const qr = qrcode(0, "M");
  qr.addData(uri);
  qr.make();

  const modulos = qr.getModuleCount();
  const partes: string[] = [];
  for (let linha = 0; linha < modulos; linha += 1) {
    for (let coluna = 0; coluna < modulos; coluna += 1) {
      if (qr.isDark(linha, coluna)) {
        partes.push(`M${coluna + MARGEM} ${linha + MARGEM}h1v1h-1z`);
      }
    }
  }

  return { lado: modulos + MARGEM * 2, caminho: partes.join("") };
}
