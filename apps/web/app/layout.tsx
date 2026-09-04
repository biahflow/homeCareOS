import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

// `next/font` baixa os arquivos da fonte em build time e os serve pelo
// próprio domínio do app — equivalente ao `@fontsource` auto-hospedado do
// portal operacional de referência: nenhuma requisição ao Google em runtime.
const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

export const metadata: Metadata = {
  title: "HomeCareOS",
  description: "Conferência de evoluções de prontuário antes do envio à operadora.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="pt-BR" className={inter.variable}>
      <body className="font-sans antialiased">{children}</body>
    </html>
  );
}
