import Link from "next/link";
import { redirect } from "next/navigation";
import { ApiError, listarOperadoras, obterDocumento } from "@homecareos/contracts";
import type { DocumentoDetalhe, Operadora, ValidacaoResumo } from "@homecareos/contracts";
import { AcaoRevalidar } from "@/components/documentos/AcaoRevalidar";
import { CAMINHO_DOCUMENTOS } from "@/components/documentos/filtros";
import { ImagemDocumento } from "@/components/documentos/ImagemDocumento";
import {
  camposDaExtracao,
  formatarConfianca,
  rotuloDeConfianca,
  varianteDeConfianca,
} from "@/components/documentos/extracao";
import {
  ROTULO_DE_STATUS_DOCUMENTO,
  ROTULO_DE_TIPO_DOCUMENTO,
  varianteDeStatus,
} from "@/components/documentos/vocabulario";
// Formatadores genéricos (`Intl` configurado com o fuso da operação) que
// nasceram na tela de relatórios. Importados, e não copiados — ver a nota na
// listagem de documentos.
import {
  formatarCompetencia,
  formatarDataHora,
  formatarInteiro,
  referenciaDoDocumento,
} from "@/components/relatorios/formatos";
import { apiUrl, opcoesAutenticadas } from "@/lib/api-servidor";
import { usuarioDaSessao } from "@/lib/sessao";

/** Uma linha do bloco de identificação. */
function Dado({ rotulo, children }: { rotulo: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-0.5">
      <dt className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">{rotulo}</dt>
      <dd className="m-0 text-sm text-ink">{children}</dd>
    </div>
  );
}

/** Variante do selo de uma validação. `reprovado` é o que pede trabalho. */
function varianteDaValidacao(resultado: ValidacaoResumo["resultado"]): string {
  return resultado === "reprovado" ? "state--3" : "state--1";
}

/** A tela de "este documento não existe", que não é a mesma coisa que um erro. */
function NaoEncontrado({ id }: { id: string }) {
  return (
    <div className="grid gap-6">
      <div className="page-head">
        <p className="eyebrow">Operação</p>
        <h1>Documento não encontrado</h1>
        <p>
          Nenhum documento com este identificador existe na API. O endereço pode estar incompleto,
          ter vindo de uma lista antiga, ou apontar para um documento removido do banco.
        </p>
      </div>

      <section className="panel">
        <p className="m-0 text-sm text-muted">
          Identificador procurado: <code className="text-xs break-all text-ink">{id}</code>
        </p>
        <Link href={CAMINHO_DOCUMENTOS} className="btn btn--secondary mt-4 w-fit">
          Voltar para os documentos
        </Link>
      </section>
    </div>
  );
}

/**
 * A conferência de um documento: a página escaneada, o que a extração leu, com
 * que confiança, o que as regras da operadora decidiram, e a revalidação.
 *
 * Server Component. Duas coisas desta tela são contrato, e não escolha de
 * layout:
 *
 * 1. **A confiança é mostrada como veio.** Ela é a incerteza do produto: a
 *    extração é feita por Vision e erra, e é a confiança por campo que diz onde
 *    olhar primeiro. Os campos vêm ordenados do menos lido para o mais lido, o
 *    número aparece junto do rótulo, e nada é arredondado para cima.
 * 2. **A autorização aqui é a da fila de pendências, não a dos relatórios.**
 *    `POST /revalidar` exige conferente ou coordenador; o gestor lê a operação e
 *    não faz conferência (ADR 0001). É o inverso do baseline de `/relatorios`,
 *    que só o gestor escreve. Esconder a ação para o gestor é ergonomia; a
 *    autoridade é o 403 da API, tratado em `AcaoRevalidar`.
 *
 * A página escaneada (`GET /api/documentos/{id}/arquivo`, PR #54) é exibida por
 * `ImagemDocumento` — Client Component pelo motivo escrito lá: o 404 de
 * "arquivo sumiu do storage" só se descobre em runtime, no `onError` do
 * `<img>`.
 */
export default async function DocumentoPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  // Memoizado por `cache` dentro desta renderização: o layout do grupo já
  // perguntou quem está logado e esta chamada reaproveita a resposta.
  const usuario = await usuarioDaSessao();
  if (usuario === null) {
    redirect("/login");
  }

  const base = apiUrl();
  const opcoes = await opcoesAutenticadas();

  let carga: [DocumentoDetalhe, Operadora[]];
  try {
    // Em paralelo: a lista de operadoras não depende do documento, e existe só
    // para dar nome ao `operadora_id` — o detalhe devolve o id, não o nome.
    carga = await Promise.all([obterDocumento(base, id, opcoes), listarOperadoras(base, opcoes)]);
  } catch (erro) {
    if (erro instanceof ApiError && erro.status === 401) {
      // A sessão morreu depois de o layout tê-la validado — expiração, logout
      // em outra aba, login novo no mesmo navegador. Por Partial Rendering o
      // layout não roda de novo a cada navegação dentro do grupo, então é esta
      // chamada que descobre e manda a pessoa de volta ao login.
      redirect("/login?motivo=sessao-encerrada");
    }
    // 404 é o id que não existe; 422 é o id que não é um UUID (a rota tipa o
    // path param como `uuid.UUID`, e `/documentos/abc` nem chega ao banco). Os
    // dois vêm de um endereço, não de uma falha do sistema, e viram a mesma
    // resposta: o endereço não aponta para documento nenhum. Tratá-los como
    // erro de servidor mandaria a pessoa procurar defeito no sistema em vez de
    // no link.
    if (erro instanceof ApiError && (erro.status === 404 || erro.status === 422)) {
      return <NaoEncontrado id={id} />;
    }
    // API fora do ar ou com defeito não vira "documento não encontrado": o erro
    // sobe.
    throw erro;
  }

  const [documento, operadoras] = carga;
  const { extracao } = documento;
  const podeRevalidar = usuario.papel !== "gestor";
  const operadora =
    documento.operadora_id === null
      ? null
      : (operadoras.find((item) => item.id === documento.operadora_id) ?? null);
  const campos = extracao === null ? [] : camposDaExtracao(extracao);
  // A API devolve as validações sem ordenar (`obter_documento` não tem
  // `order_by`): a ordem seria a que o Postgres entregasse. Aqui é a mais
  // recente primeiro, que é como se lê um histórico.
  //
  // Comparação de string crua, e não `localeCompare` nem `Date.parse`: são ISO
  // 8601 no mesmo fuso, formato em que a ordem lexicográfica **é** a ordem
  // cronológica. `localeCompare` é colação humana (pode tratar pontuação como
  // variável) e `Date.parse` devolveria `NaN` num timestamp malformado, o que
  // deixaria a ordenação indefinida em vez de estável.
  const validacoes = [...documento.validacoes].sort((a, b) =>
    a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0,
  );

  return (
    <div className="grid gap-6">
      <div className="page-head">
        <p className="eyebrow">Operação</p>
        <h1>Documento {referenciaDoDocumento(documento.id)}</h1>
        <p>
          Conferência de uma evolução de prontuário: o que a extração leu, com que confiança, e o
          que as regras da operadora decidiram sobre ela.
        </p>
      </div>

      <div>
        <Link href={CAMINHO_DOCUMENTOS} className="btn btn--ghost w-fit px-3 text-xs">
          ← Voltar para os documentos
        </Link>
      </div>

      <section className="panel">
        <div className="panel-heading">
          <h2>Identificação</h2>
          <span className={`state ${varianteDeStatus(documento.status)}`}>
            {ROTULO_DE_STATUS_DOCUMENTO[documento.status]}
          </span>
        </div>

        <dl className="m-0 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <Dado rotulo="Tipo">{ROTULO_DE_TIPO_DOCUMENTO[documento.tipo]}</Dado>
          <Dado rotulo="Competência">
            <span className="first-letter:uppercase">
              {formatarCompetencia(documento.competencia)}
            </span>{" "}
            <span className="text-xs text-muted">({documento.competencia})</span>
          </Dado>
          <Dado rotulo="Página">
            {/* Página nula não é página zero: o documento não veio de um PDF
                multi-página. */}
            {documento.pagina === null ? "única" : formatarInteiro(documento.pagina)}
          </Dado>
          <Dado rotulo="Operadora">
            {documento.operadora_id === null ? (
              <span className="text-muted">
                sem operadora — <span className="text-xs">sem ela não há regra a aplicar</span>
              </span>
            ) : (
              (operadora?.nome ?? "operadora não encontrada na lista")
            )}
          </Dado>
          <Dado rotulo="Paciente">
            {documento.paciente_id === null ? (
              <span className="text-muted">não vinculado</span>
            ) : (
              <code className="text-xs" title={documento.paciente_id}>
                {referenciaDoDocumento(documento.paciente_id)}
              </code>
            )}
          </Dado>
          <Dado rotulo="Recebido em">{formatarDataHora(documento.created_at)}</Dado>
          <Dado rotulo="Atualizado em">{formatarDataHora(documento.updated_at)}</Dado>
          <Dado rotulo="Identificador">
            <code className="text-xs break-all">{documento.id}</code>
          </Dado>
        </dl>
      </section>

      <section className="panel">
        <div className="panel-heading">
          <h2>Documento escaneado</h2>
        </div>

        {/* A imagem é o objeto do trabalho desta tela — quem confere veio
            comparar o papel com o que a extração leu — então ganha uma seção
            própria com espaço de verdade, não uma miniatura ao lado dos
            metadados. */}
        <ImagemDocumento documentoId={documento.id} />
      </section>

      <section className="panel">
        <div className="panel-heading">
          <h2>Extração</h2>
          {extracao !== null && (
            <span className={`state ${varianteDeConfianca(extracao.confianca)}`}>
              Confiança {formatarConfianca(extracao.confianca)}
            </span>
          )}
        </div>

        {extracao === null ? (
          <p className="empty-state">
            Nenhuma extração registrada para este documento. Não há campo para conferir, e a
            revalidação também não é possível sem ela — ela reaplica regras sobre uma extração
            existente, não extrai de novo.
          </p>
        ) : (
          <>
            <p className="m-0 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
              <span>
                Provider: <strong className="text-ink">{extracao.provider || "não informado"}</strong>
              </span>
              <span>
                Modelo: <strong className="text-ink">{extracao.modelo || "não informado"}</strong>
              </span>
              <span>Extraído em {formatarDataHora(extracao.created_at)}</span>
            </p>

            {/* Sem chave da Anthropic configurada, a extração não roda: todo
                campo entra como ilegível e a confiança sai 0. É configuração,
                não documento ruim — e apresentar esses zeros sem dizer isso
                acusaria o documento pelo que é decisão de ambiente. */}
            {extracao.provider === "null" && (
              <p className="alert--info mt-4">
                {/* Um `<span>` só: `.alert--info` é flex, e texto solto ao lado
                    de um elemento vira dois itens de flex lado a lado — a frase
                    sairia partida no meio. */}
                <span>
                  Esta extração veio do provider <code>null</code>: sem chave de IA configurada
                  neste ambiente, nenhum campo foi lido e a confiança é 0 por construção. Os campos
                  abaixo não descrevem o documento — descrevem a ausência de extração.
                </span>
              </p>
            )}

            <p className="mt-5 mb-3 text-[11px] font-bold uppercase tracking-[0.18em] text-muted">
              Campos, do menos lido para o mais lido
            </p>

            {campos.length === 0 ? (
              <p className="empty-state">
                A extração existe mas não trouxe campo nenhum. Não é tela vazia: é o que a API
                devolveu.
              </p>
            ) : (
              <ul className="m-0 flex list-none flex-col divide-y divide-line p-0">
                {campos.map((campo) => (
                  <li
                    key={campo.nome}
                    // `minmax(0, …)` nas duas primeiras colunas: sem isso o
                    // mínimo é o conteúdo, e uma chave longa como
                    // `assinatura_paciente_responsavel_presente` invade a
                    // coluna do valor em vez de quebrar dentro da sua.
                    className="grid gap-1.5 py-3 first:pt-0 last:pb-0 sm:grid-cols-[minmax(0,14rem)_minmax(0,1fr)_auto] sm:items-baseline sm:gap-4"
                  >
                    {/* A chave crua, como a validação a nomeia ("Campo
                        'carimbo_legivel' foi marcado como ilegível"). */}
                    <code className="text-xs break-all text-muted">{campo.nome}</code>
                    <span
                      className={`text-sm break-words ${
                        campo.semConteudo ? "text-muted italic" : "text-ink"
                      }`}
                    >
                      {campo.valor}
                    </span>
                    {campo.confianca === null ? (
                      <span className="state state--off">confiança não medida</span>
                    ) : (
                      <span className={`state ${varianteDeConfianca(campo.confianca)}`}>
                        {rotuloDeConfianca(campo.confianca)} · {formatarConfianca(campo.confianca)}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}

            <p className="alert--info mt-4">
              A confiança é declarada pelo próprio modelo que leu a página, em três níveis: 0% para
              campo que ele não conseguiu ler, 50% para campo lido com dúvida e 100% para o resto. A
              faixa do meio é a que vale conferir contra o papel. A confiança do topo é a média
              simples destes campos.
            </p>
          </>
        )}
      </section>

      <section className="panel">
        <div className="panel-heading">
          <h2>Validações</h2>
          <span className="state state--off">
            {formatarInteiro(validacoes.length)}{" "}
            {validacoes.length === 1 ? "registro" : "registros"}
          </span>
        </div>

        {validacoes.length === 0 ? (
          <p className="empty-state">
            Nenhuma regra foi aplicada a este documento ainda. Sem operadora vinculada ou sem regra
            ativa para ela, não há o que validar.
          </p>
        ) : (
          <>
            <p className="alert--info mb-4">
              <span>
                Esta lista é o <strong>histórico</strong>, do mais recente para o mais antigo: cada
                revalidação acrescenta uma linha por regra ativa e não apaga as anteriores. A mesma
                regra aparecendo várias vezes é o documento tendo sido revalidado, não duplicidade.
              </span>
            </p>

            <ul className="m-0 flex list-none flex-col divide-y divide-line p-0">
              {validacoes.map((validacao) => (
                <li key={validacao.id} className="grid gap-1.5 py-3 first:pt-0 last:pb-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={`state ${varianteDaValidacao(validacao.resultado)}`}>
                      {validacao.resultado === "reprovado" ? "Reprovado" : "Aprovado"}
                    </span>
                    <span className="text-sm text-ink">{validacao.detalhe}</span>
                  </div>
                  <p className="m-0 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
                    <span>{formatarDataHora(validacao.created_at)}</span>
                    {/* O id da regra é referência técnica: `GET /api/regras`
                        exige coordenador, e resolver o nome aqui daria 403 na
                        tela da conferente — que é justamente quem mais lê esta
                        lista. */}
                    <span title={`Regra ${validacao.regra_id}`}>
                      Regra {referenciaDoDocumento(validacao.regra_id)}
                    </span>
                  </p>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      <section className="panel">
        <div className="panel-heading">
          <h2>Revalidação</h2>
        </div>

        {podeRevalidar ? (
          <AcaoRevalidar documentoId={documento.id} />
        ) : (
          <p className="alert--info">
            Seu papel (gestor) vê a conferência inteira, mas não revalida documentos: revalidar é
            ação de conferência, feita por conferente ou coordenador (ADR 0001).
          </p>
        )}
      </section>
    </div>
  );
}
