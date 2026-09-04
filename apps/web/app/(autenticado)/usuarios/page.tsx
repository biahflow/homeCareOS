import Link from "next/link";
import { redirect } from "next/navigation";
import { ApiError, listarUsuarios } from "@homecareos/contracts";
import type { RespostaPaginada, UsuarioOut } from "@homecareos/contracts";
import { PAPEL_LABEL } from "@/components/shell/usuario";
import { AcaoUsuario } from "@/components/usuarios/AcaoUsuario";
import {
  LIMITE_POR_PAGINA,
  ROTULO_DE_SITUACAO,
  lerFiltros,
  temFiltro,
  urlComFiltros,
} from "@/components/usuarios/filtros";
import { FiltrosUsuarios } from "@/components/usuarios/FiltrosUsuarios";
import { FormularioNovoUsuario } from "@/components/usuarios/FormularioNovoUsuario";
import { apiUrl, opcoesAutenticadas } from "@/lib/api-servidor";
import { usuarioDaSessao } from "@/lib/sessao";

/**
 * Administração de usuários — quem entra no sistema e com que papel.
 *
 * Server Component, com o filtro na URL (`searchParams`) — ver a docstring de
 * `components/usuarios/filtros.ts`. Três coisas desta tela são contrato, e não
 * escolha de layout:
 *
 * 1. **A tela inteira é do coordenador** (ADR 0004): `/api/usuarios` é montada
 *    com `exigir_papel(Papel.COORDENADOR)` e responde 403 a conferente e gestor
 *    já na listagem. Diferente de `/documentos` e `/pendencias`, que os três
 *    papéis leem — aqui não há nada para ler sem o papel. Daí a recusa antes de
 *    qualquer chamada, abaixo.
 * 2. **Não há exclusão.** A API não tem `DELETE` nesta rota porque a auditoria
 *    referencia o usuário; desativar é o que existe, e ele derruba as sessões
 *    abertas da pessoa na hora.
 * 3. **A ordem é do servidor** (`nome`, depois `email`) e não é parametrizável.
 *    Não há controle de ordenação nesta tela, e reordenar aqui desfaria a
 *    estabilidade que o desempate por e-mail dá à paginação por offset.
 */
export default async function UsuariosPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const filtros = lerFiltros(await searchParams);
  // Memoizado por `cache` dentro desta renderização: o layout do grupo já
  // perguntou quem está logado e esta chamada reaproveita a resposta.
  const usuario = await usuarioDaSessao();
  if (usuario === null) {
    redirect("/login");
  }

  // Esconder o item na navegação é conveniência; **esta linha é a recusa**. Ela
  // vem antes de qualquer chamada à API: quem digitou o endereço não é
  // coordenador, não vê a tela, e nem chega a gerar uma requisição que a API
  // recusaria. Não substitui o 403 da API (papel alterado no servidor entre esta
  // leitura e a chamada), que é tratado abaixo.
  //
  // Redirecionamento com aviso, e não `notFound()`: quem esbarra aqui é uma
  // colega autenticada, não alguém sondando o sistema. A existência da rota já
  // está documentada no ADR 0004, e o 404 do framework — não há `not-found.tsx`
  // neste app — jogaria a pessoa para fora da shell autenticada, numa página
  // sem volta que faz um sistema funcionando parecer quebrado. O aviso é o mesmo
  // mecanismo do `?motivo=` que a tela de login já usa.
  if (usuario.papel !== "coordenador") {
    redirect("/documentos?motivo=usuarios-restrito");
  }

  const base = apiUrl();
  const opcoes = await opcoesAutenticadas();

  let pagina: RespostaPaginada<UsuarioOut>;
  try {
    pagina = await listarUsuarios(
      base,
      { ativo: filtros.ativo, limite: LIMITE_POR_PAGINA, offset: filtros.offset },
      opcoes,
    );
  } catch (erro) {
    if (erro instanceof ApiError && erro.status === 401) {
      // A sessão morreu depois de o layout tê-la validado — expiração, logout
      // em outra aba, login novo no mesmo navegador. Por Partial Rendering o
      // layout não roda de novo a cada navegação dentro do grupo, então é esta
      // chamada que descobre e manda a pessoa de volta ao login.
      redirect("/login?motivo=sessao-encerrada");
    }
    if (erro instanceof ApiError && erro.status === 403) {
      // O papel mudou no servidor depois de esta requisição ler quem é a
      // pessoa — outro coordenador a rebaixou no meio do turno. A tela não tem
      // um pedaço que sobreviva sem a listagem, então a saída é a mesma da
      // recusa acima, e não um erro de servidor.
      redirect("/documentos?motivo=usuarios-restrito");
    }
    // API fora do ar ou com defeito não vira tela vazia fingindo que não há
    // usuário: o erro sobe.
    throw erro;
  }

  const { total } = pagina.paginacao;
  const filtrando = temFiltro(filtros);
  const primeiroDaPagina = filtros.offset + 1;
  const ultimoDaPagina = filtros.offset + pagina.data.length;

  return (
    <div className="grid gap-6">
      <div className="page-head">
        {/* "Operação", e não "Administração", porque o `eyebrow` de toda tela
            deste app repete o prefixo que o breadcrumb da `Topbar` monta a
            partir de `NAV_ITEMS` — e `/usuarios` entrou nesse grupo. Duas
            palavras diferentes a 40px uma da outra na mesma tela seriam lidas
            como duas seções. */}
        <p className="eyebrow">Operação</p>
        <h1>Usuários</h1>
        <p>
          Quem entra no sistema e com que papel. Contas não são apagadas — elas continuam
          respondendo por quem fez o quê no histórico de conferência; desativar é o que tira o
          acesso.
        </p>
      </div>

      <FormularioNovoUsuario />

      <FiltrosUsuarios filtros={filtros} />

      <section className="panel">
        <div className="panel-heading">
          <h2>{filtrando ? "Usuários filtrados" : "Todos os usuários"}</h2>
          <span className="state state--off">
            {total} {total === 1 ? "usuário" : "usuários"}
          </span>
        </div>

        {total === 0 && filtrando && (
          <div className="grid justify-items-center gap-3">
            <p className="empty-state w-full">
              Nenhum usuário com este filtro. Existem usuários no cadastro — a contagem acima é a do
              recorte em vigor.
            </p>
            <Link href={urlComFiltros({ offset: 0 })} className="btn btn--secondary">
              Limpar filtros
            </Link>
          </div>
        )}

        {total > 0 && pagina.data.length === 0 && (
          <div className="grid justify-items-center gap-3">
            <p className="empty-state w-full">
              Esta página não tem mais itens: o cadastro encurtou desde que este endereço foi
              aberto.
            </p>
            <Link href={urlComFiltros({ ...filtros, offset: 0 })} className="btn btn--secondary">
              Voltar à primeira página
            </Link>
          </div>
        )}

        {pagina.data.length > 0 && (
          <ul className="m-0 flex list-none flex-col divide-y divide-line p-0">
            {pagina.data.map((pessoa) => {
              const ehVoce = pessoa.id === usuario.id;

              return (
                <li
                  key={pessoa.id}
                  className="grid gap-3 py-4 first:pt-0 last:pb-0 sm:grid-cols-[1fr_auto] sm:items-start sm:gap-6"
                >
                  <div className="grid gap-1.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`state ${pessoa.ativo ? "state--1" : "state--off"}`}>
                        {pessoa.ativo ? ROTULO_DE_SITUACAO.ativo : ROTULO_DE_SITUACAO.desativado}
                      </span>
                      <strong className="text-sm text-ink">{pessoa.nome}</strong>
                      {ehVoce && <span className="state state--2">Você</span>}
                    </div>

                    <p className="m-0 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
                      <span>{pessoa.email}</span>
                      <span>
                        Papel: <strong className="text-ink">{PAPEL_LABEL[pessoa.papel]}</strong>
                      </span>
                    </p>

                    {!pessoa.ativo && (
                      <p className="m-0 text-xs leading-5 text-muted">
                        Sem acesso ao sistema. As sessões desta conta foram encerradas quando ela
                        foi desativada.
                      </p>
                    )}
                  </div>

                  <AcaoUsuario
                    usuarioId={pessoa.id}
                    nome={pessoa.nome}
                    papel={pessoa.papel}
                    ativo={pessoa.ativo}
                    ehVoce={ehVoce}
                  />
                </li>
              );
            })}
          </ul>
        )}

        {total > LIMITE_POR_PAGINA && pagina.data.length > 0 && (
          <nav
            aria-label="Paginação"
            className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4"
          >
            <p className="m-0 text-xs text-muted">
              Mostrando {primeiroDaPagina}–{ultimoDaPagina} de {total}
            </p>
            <div className="flex gap-2">
              {filtros.offset > 0 ? (
                <Link
                  href={urlComFiltros({
                    ...filtros,
                    offset: Math.max(0, filtros.offset - LIMITE_POR_PAGINA),
                  })}
                  className="btn btn--secondary h-9 min-h-9 px-3 text-xs"
                >
                  Anterior
                </Link>
              ) : (
                <span
                  aria-disabled="true"
                  className="btn btn--secondary h-9 min-h-9 cursor-not-allowed px-3 text-xs opacity-60"
                >
                  Anterior
                </span>
              )}

              {ultimoDaPagina < total ? (
                <Link
                  href={urlComFiltros({
                    ...filtros,
                    offset: filtros.offset + LIMITE_POR_PAGINA,
                  })}
                  className="btn btn--secondary h-9 min-h-9 px-3 text-xs"
                >
                  Próxima
                </Link>
              ) : (
                <span
                  aria-disabled="true"
                  className="btn btn--secondary h-9 min-h-9 cursor-not-allowed px-3 text-xs opacity-60"
                >
                  Próxima
                </span>
              )}
            </div>
          </nav>
        )}
      </section>
    </div>
  );
}
