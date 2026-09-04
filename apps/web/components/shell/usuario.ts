import type { Papel } from "@homecareos/contracts";

/**
 * O que a shell mostra de quem está logado — e nada além.
 *
 * Não é o `UsuarioOut` inteiro de propósito: `id`, `email` e `ativo` não têm o
 * que fazer numa barra de navegação, e o que não é passado para o Client
 * Component não viaja no payload que o navegador recebe.
 */
export interface UsuarioDaShell {
  nome: string;
  papel: Papel;
}

/** Rótulo de exibição dos papéis do ADR 0001. */
export const PAPEL_LABEL: Record<Papel, string> = {
  conferente: "Conferente",
  coordenador: "Coordenador",
  gestor: "Gestor",
};
