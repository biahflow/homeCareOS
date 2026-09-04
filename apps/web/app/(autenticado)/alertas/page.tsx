import Link from "next/link";
import { redirect } from "next/navigation";
import { ApiError, listarAlertas } from "@homecareos/contracts";
import type { AlertaItem, RespostaPaginada } from "@homecareos/contracts";
import {
  LIMITE_POR_PAGINA,
  lerFiltros,
  rotuloDoStatus,
  rotuloDoTipo,
  temFiltro,
  urlComFiltros,
  varianteDoStatus,
} from "@/components/alertas/filtros";
import { FiltrosAlertas } from "@/components/alertas/FiltrosAlertas";
// Reuso, e não cópia, do endereço de um documento: a mesma função que a
// listagem de documentos usa para montar o próprio link.
import { urlDoDocumento } from "@/components/documentos/filtros";
// Mesmo `Intl` configurado com o fuso da operação que o resto do app usa —
// duas configurações divergiriam na primeira mudança.
import { formatarDataHora } from "@/components/relatorios/formatos";
import { apiUrl, opcoesAutenticadas } from "@/lib/api-servidor";
import { usuarioDaSessao } from "@/lib/sessao";

/**
 * O log de alertas de WhatsApp: quem foi avisado, do quê, e o que aconteceu.
 *
 * Server Component, com o filtro na URL (`searchParams`) — ver a docstring de
 * `components/alertas/filtros.ts`. Duas coisas desta tela são contrato, e não
 * escolha de layout:
 *
 * 1. **A tela é de coordenador e gestor** — `/api/alertas` é montada com
 *    `exigir_papel(Papel.COORDENADOR, Papel.GESTOR)` desde a issue #30, e
 *    responde 403 a conferente já na listagem. Daí a recusa antes de qualquer
 *    chamada, abaixo, igual à de `/usuarios`.
 * 2. **O log não é o registro completo de toda supressão.** A supressão por
 *    cooldown (mesmo assunto, mesmo destinatário, 24h) não grava linha
 *    nenhuma — só a supressão por rate limit grava. O aviso abaixo do título
 *    existe para ninguém concluir, pela ausência de uma linha, que um alerta
 *    foi enviado quando na verdade ele pode ter sido apenas represado por
 *    cooldown. Ver a docstring de `listarAlertas` no cliente para o
 *    detalhamento das duas defesas.
 * 3. **A ordem é do servidor** (`created_at` decrescente) e não é
 *    parametrizável — não há controle de ordenação nesta tela.
 */
export default async function AlertasPage({
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
  // coordenador nem gestor, não vê a tela, e nem chega a gerar uma requisição
  // que a API recusaria. Não substitui o 403 da API (papel alterado no
  // servidor entre esta leitura e a chamada), que é tratado abaixo.
  if (usuario.papel !== "coordenador" && usuario.papel !== "gestor") {
    redirect("/documentos?motivo=alertas-restrito");
  }

  const base = apiUrl();
  const opcoes = await opcoesAutenticadas();

  let pagina: RespostaPaginada<AlertaItem>;
  try {
    pagina = await listarAlertas(
      base,
      {
        tipo: filtros.tipo,
        status: filtros.status,
        documento_id: filtros.documento_id,
        limite: LIMITE_POR_PAGINA,
        offset: filtros.offset,
      },
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
      redirect("/documentos?motivo=alertas-restrito");
    }
    // API fora do ar ou com defeito não vira tela vazia fingindo que não há
    // alerta: o erro sobe.
    throw erro;
  }

  const { total } = pagina.paginacao;
  const filtrando = temFiltro(filtros);
  const primeiroDaPagina = filtros.offset + 1;
  const ultimoDaPagina = filtros.offset + pagina.data.length;

  return (
    <div className="grid gap-6">
      <div className="page-head">
        <p className="eyebrow">Operação</p>
        <h1>Alertas</h1>
        <p>
          O que o sistema avisou no WhatsApp da equipe — documento incompleto, prazo de
          competência, volume anormal e pendência parada.
        </p>
      </div>

      {/* Este parágrafo é o ponto central da tela, não um rodapé: quem chega
          aqui perguntando "por que não fui avisado" precisa ler isto antes de
          concluir qualquer coisa a partir de uma linha que não encontrou. */}
      <p role="status" className="alert--info">
        Este log não registra toda supressão. Quando o mesmo assunto já foi avisado ao mesmo
        número nas últimas 24 horas (cooldown), o envio é pulado sem gravar linha aqui — de
        propósito, para a tabela não virar ruído. Só a supressão por teto de mensagens por hora
        (rate limit) fica registrada, porque essa é a anômala. Por isso, não encontrar um alerta
        aqui não prova que ele foi enviado.
      </p>

      <FiltrosAlertas filtros={filtros} />

      <section className="panel">
        <div className="panel-heading">
          <h2>{filtrando ? "Alertas filtrados" : "Todos os alertas"}</h2>
          <span className="state state--off">
            {total} {total === 1 ? "alerta" : "alertas"}
          </span>
        </div>

        {total === 0 && filtrando && (
          <div className="grid justify-items-center gap-3">
            {/* Não afirma que existem alertas fora do filtro: com um recorte
                aplicado, esta tela não sabe se o log tem outras linhas ou se
                está vazio, e chutar seria contradizer o aviso do topo — que
                existe justamente para ninguém concluir coisa nenhuma a partir
                de uma ausência. */}
            <p className="empty-state w-full">
              Nenhum alerta com este filtro. A contagem acima é a do recorte em vigor; limpe os
              filtros para ver o log inteiro.
            </p>
            <Link href={urlComFiltros({ offset: 0 })} className="btn btn--secondary">
              Limpar filtros
            </Link>
          </div>
        )}

        {total === 0 && !filtrando && (
          <p className="empty-state w-full">
            Nenhum alerta enviado ainda. Lembre-se: a ausência de linhas aqui também é o estado
            normal quando toda supressão recente foi por cooldown — ver o aviso acima.
          </p>
        )}

        {total > 0 && pagina.data.length === 0 && (
          <div className="grid justify-items-center gap-3">
            <p className="empty-state w-full">
              Esta página não tem mais itens: o log encurtou desde que este endereço foi aberto.
            </p>
            <Link href={urlComFiltros({ ...filtros, offset: 0 })} className="btn btn--secondary">
              Voltar à primeira página
            </Link>
          </div>
        )}

        {pagina.data.length > 0 && (
          <ul className="m-0 flex list-none flex-col divide-y divide-line p-0">
            {pagina.data.map((alerta) => (
              <li key={alerta.id} className="grid gap-1.5 py-4 first:pt-0 last:pb-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`state ${varianteDoStatus(alerta.status)}`}>
                    {rotuloDoStatus(alerta.status)}
                  </span>
                  <strong className="text-sm text-ink">{rotuloDoTipo(alerta.tipo)}</strong>
                  <span className="text-xs text-muted">{formatarDataHora(alerta.created_at)}</span>
                </div>

                <p className="m-0 text-xs text-muted">
                  Destinatário: <span className="text-ink">{alerta.destinatario}</span>
                </p>

                <p className="m-0 text-sm text-ink">{alerta.mensagem}</p>

                {alerta.detalhe !== null && (
                  <p className="m-0 text-xs leading-5 text-muted">
                    {/* Três casos, e não dois: `detalhe` é nullable para
                        qualquer status, então um alerta `enviado` que traga
                        texto aqui não pode ser rotulado como "motivo da
                        supressão" — que é o que uma condição binária faria. */}
                    {alerta.status === "falha"
                      ? "Motivo da falha: "
                      : alerta.status === "suprimido"
                        ? "Motivo da supressão: "
                        : "Detalhe: "}
                    {alerta.detalhe}
                  </p>
                )}

                {alerta.documento_id !== null && (
                  <Link
                    href={urlDoDocumento(alerta.documento_id)}
                    className="btn btn--secondary h-8 min-h-8 w-fit px-3 text-xs"
                  >
                    Ver documento
                  </Link>
                )}
              </li>
            ))}
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
