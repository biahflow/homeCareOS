import Link from "next/link";
import { redirect } from "next/navigation";
import { ApiError, ehAtorMaquina, listarAuditoriaCanais, listarCanais } from "@homecareos/contracts";
import type { AuditoriaCanalItem, CanalOut, RespostaPaginada } from "@homecareos/contracts";
import { AcaoCanal } from "@/components/alertas/AcaoCanal";
import {
  LIMITE_HISTORICO,
  SELO_DE_ESTADO,
  credencialDoCanal,
  desligarSilenciaTudo,
  estadoDoCanal,
  fraseDaProcedencia,
  lerOffsetDoHistorico,
  urlDoHistorico,
} from "@/components/alertas/canais";
// Reuso, e não cópia, do endereço do log e do rótulo de canal: a mesma função
// que a listagem de alertas usa para nomear por onde a mensagem saiu.
import { CAMINHO_ALERTAS, rotuloDoCanal } from "@/components/alertas/filtros";
// Mesmo `Intl` configurado com o fuso da operação que o resto do app usa —
// duas configurações divergiriam na primeira mudança.
import { formatarDataHora } from "@/components/relatorios/formatos";
import { apiUrl, opcoesAutenticadas } from "@/lib/api-servidor";
import { usuarioDaSessao } from "@/lib/sessao";

/**
 * A frase que explica, para este canal, o que está acontecendo agora.
 *
 * Quatro combinações e não três: `desligado` se divide em "a credencial está
 * lá" e "a credencial também falta", porque a diferença muda o que a pessoa
 * deve fazer a seguir — num caso ligar aqui resolve, no outro ligar aqui não
 * resolve nada e alguém precisa mexer no servidor. Dizer isso **antes** do
 * clique é o que impede a descoberta pelo caminho ruim: ligar, esperar o aviso
 * e não receber.
 */
function FraseDoEstado({ canal }: { canal: CanalOut }) {
  const credencial = credencialDoCanal(canal.canal) ?? "as credenciais deste canal";

  if (canal.habilitado && canal.disponivel) {
    return (
      <p className="m-0 text-sm leading-6 text-ink">
        Este canal <strong>está enviando</strong>: ele está ligado e as credenciais dele estão no
        servidor.
      </p>
    );
  }

  if (canal.habilitado) {
    return (
      <p className="m-0 text-sm leading-6 text-ink">
        Este canal está ligado, mas <strong>não envia nada</strong>: faltam {credencial} no
        servidor. <strong>Isso não se resolve por aqui</strong> — credencial vive no `.env` do
        servidor e mudá-la é deploy. Enquanto faltar, ligado e desligado dão no mesmo resultado.
      </p>
    );
  }

  if (canal.disponivel) {
    return (
      <p className="m-0 text-sm leading-6 text-ink">
        Este canal <strong>está desligado</strong>: nenhum aviso sai por ele. As credenciais dele
        estão no servidor, então ligar aqui basta para ele voltar a enviar.
      </p>
    );
  }

  return (
    <p className="m-0 text-sm leading-6 text-ink">
      Este canal <strong>está desligado</strong>: nenhum aviso sai por ele. E ligá-lo não faria
      nada sair — também faltam {credencial} no servidor, o que não se resolve por aqui.
    </p>
  );
}

/**
 * Os canais de alerta: por onde a operação é avisada, e por onde não é.
 *
 * Server Component. **Sub-rota de `/alertas`, e não uma área de configurações
 * nova**: quem olha o log é quem liga e desliga, `/api/alertas/canais` vive sob
 * o mesmo prefixo pela mesma razão, e uma área `/configuracoes` com um item só
 * dentro é pior que nenhuma. Por ser sub-rota, a shell já a trata como parte de
 * Alertas — `NavList` e `Topbar` casam por `startsWith(href + "/")` —, e por
 * isso ela **não** ganha item próprio em `nav-items.ts`: o caminho de ida é o
 * link daqui para o log e do log para cá.
 *
 * Quatro coisas desta tela são contrato, e não escolha de layout:
 *
 * 1. **`habilitado` e `disponivel` aparecem separados, sempre.** É o requisito
 *    literal do ADR 0006 — juntá-los num único "ativo" apaga a diferença entre
 *    "desliguei" e "esqueci de configurar", e é o que faz alguém ligar um canal
 *    e não entender por que nada sai.
 * 2. **A tela é de coordenador e gestor, e só o coordenador altera.** Isto é
 *    diferente das telas anteriores, onde o papel decidia se a tela abria: aqui
 *    os dois papéis entram e um deles não escreve. Esconder o controle do
 *    gestor é ergonomia; **a autoridade é o 403 da API** (ver `atualizarCanal`).
 *    Conferente não tem o que ler e é recusado abaixo.
 * 3. **Estado sem autor não vira autor inventado.** `atualizado_por` nulo é o
 *    valor semeado pela migração de configuração, e a tela diz isso com essas
 *    palavras — nada de "sistema" nem "automático". Ver `fraseDaProcedencia`.
 * 4. **O histórico fica nesta tela, junto do estado.** É ele que responde
 *    "desde quando estamos sem aviso?", e numa tela separada ninguém abriria.
 */
export default async function CanaisDeAlertaPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const offset = lerOffsetDoHistorico(await searchParams);
  // Memoizado por `cache` dentro desta renderização: o layout do grupo já
  // perguntou quem está logado e esta chamada reaproveita a resposta.
  const usuario = await usuarioDaSessao();
  if (usuario === null) {
    redirect("/login");
  }

  // Esconder o item na navegação é conveniência; **esta linha é a recusa**. Ela
  // vem antes de qualquer chamada à API: quem digitou o endereço não é
  // coordenador nem gestor, não vê a tela, e nem chega a gerar uma requisição
  // que a API recusaria. Não substitui o 403 da API (papel alterado no servidor
  // entre esta leitura e a chamada), que é tratado abaixo.
  if (usuario.papel !== "coordenador" && usuario.papel !== "gestor") {
    redirect("/documentos?motivo=canais-restrito");
  }

  // Ergonomia, não proteção: o gestor lê a operação inteira e não a executa
  // (ADR 0001, reafirmado pelo ADR 0006), e ligar canal é executá-la. Quem
  // recusa de verdade é o `PATCH`, com 403.
  const podeAlterar = usuario.papel === "coordenador";

  const base = apiUrl();
  const opcoes = await opcoesAutenticadas();

  let carga: [CanalOut[], RespostaPaginada<AuditoriaCanalItem>];
  try {
    // Em paralelo: o histórico não depende do estado atual, e em série a tela
    // esperaria duas vezes o mesmo tempo de rede. As duas rotas têm a mesma
    // autorização (a do router), então não há o caso de uma passar e a outra
    // não que justificasse embrulhar uma delas.
    carga = await Promise.all([
      listarCanais(base, opcoes),
      listarAuditoriaCanais(base, { limite: LIMITE_HISTORICO, offset }, opcoes),
    ]);
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
      // pessoa. A tela não tem um pedaço que sobreviva sem o estado dos canais,
      // então a saída é a mesma da recusa acima, e não um erro de servidor.
      redirect("/documentos?motivo=canais-restrito");
    }
    // **Nunca vire "nenhum canal".** Uma tela vazia aqui afirmaria que a
    // operação não tem canal nenhum ligado, que é a conclusão errada mais cara
    // que esta tela pode induzir. O erro sobe.
    throw erro;
  }

  const [canais, historico] = carga;
  const { total } = historico.paginacao;
  const primeiroDaPagina = offset + 1;
  const ultimoDaPagina = offset + historico.data.length;

  return (
    <div className="grid gap-6">
      <div className="page-head">
        <p className="eyebrow">Operação</p>
        <h1>Canais de alerta</h1>
        <p>
          Por onde a operação é avisada — e por onde não é. O que já saiu continua no log de
          alertas; aqui fica a decisão de quais canais enviam daqui para a frente.
        </p>
      </div>

      {/* Este parágrafo é o ponto central da tela, não um rodapé: é ele que
          impede alguém de ler um único "ligado" e concluir que o canal envia. */}
      <p role="status" className="alert--info">
        Um canal só envia quando as <strong>duas</strong> respostas são sim:{" "}
        <strong>ligado</strong>, que é a decisão de quem opera e muda aqui, e{" "}
        <strong>credencial no servidor</strong>, que vive no `.env` e só muda por deploy.
        Enquanto uma delas for não, nada sai por esse canal — e ninguém é avisado disso: a
        ausência de alerta é indistinguível de &ldquo;não havia o que alertar&rdquo;, o mesmo
        que o log de alertas já avisa sobre o cooldown.
      </p>

      <section className="panel">
        <div className="panel-heading">
          <h2>Canais</h2>
          <Link
            href={CAMINHO_ALERTAS}
            className="btn btn--secondary h-9 min-h-9 shrink-0 px-3 text-xs"
          >
            Ver o log de alertas
          </Link>
        </div>

        <ul className="m-0 flex list-none flex-col divide-y divide-line p-0">
          {canais.map((canal) => {
            const selo = SELO_DE_ESTADO[estadoDoCanal(canal)];
            return (
              <li key={canal.canal} className="grid gap-2.5 py-4 first:pt-0 last:pb-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`state ${selo.variante}`}>{selo.rotulo}</span>
                  <strong className="text-sm text-ink">{rotuloDoCanal(canal.canal)}</strong>
                </div>

                {/* As duas perguntas, nomeadas e lado a lado — nunca fundidas
                    num "ativo" só. Ver o item 1 da docstring da página. */}
                <dl className="m-0 flex flex-wrap gap-x-6 gap-y-1 text-xs">
                  <div className="flex gap-1.5">
                    <dt className="m-0 text-muted">Ligado</dt>
                    <dd className="m-0 font-semibold text-ink">
                      {canal.habilitado ? "sim" : "não"}
                    </dd>
                  </div>
                  <div className="flex gap-1.5">
                    <dt className="m-0 text-muted">Credenciais no servidor</dt>
                    <dd className="m-0 font-semibold text-ink">
                      {canal.disponivel ? "sim" : "não"}
                    </dd>
                  </div>
                </dl>

                <FraseDoEstado canal={canal} />

                <p className="m-0 text-xs leading-5 text-muted">{fraseDaProcedencia(canal)}</p>

                {podeAlterar && (
                  <AcaoCanal
                    canal={canal}
                    silenciaTudo={desligarSilenciaTudo(canais, canal.canal)}
                  />
                )}
              </li>
            );
          })}
        </ul>

        {!podeAlterar && (
          <p className="mt-4 text-xs leading-5 text-muted">
            Seu papel lê o estado e o histórico dos canais, mas não os altera: ligar e desligar
            canal é operação, e quem opera é o coordenador. Se um canal precisa mudar, peça a um
            deles.
          </p>
        )}
      </section>

      <section className="panel">
        <div className="panel-heading">
          <h2>Histórico de mudanças</h2>
          <span className="state state--off">
            {total} {total === 1 ? "mudança" : "mudanças"}
          </span>
        </div>

        {/* É este histórico que responde "desde quando estamos sem aviso?" — a
            pergunta que vai ser feita quando alguém reparar que parou de
            receber. */}
        {total === 0 && (
          <p className="empty-state w-full">
            Nenhuma mudança registrada: nenhum canal foi ligado nem desligado desde que este
            histórico existe. O estado acima é o que a instalação semeou.
          </p>
        )}

        {total > 0 && historico.data.length === 0 && (
          <div className="grid justify-items-center gap-3">
            <p className="empty-state w-full">
              Esta página não tem mais itens: o histórico encurtou desde que este endereço foi
              aberto.
            </p>
            <Link href={urlDoHistorico(0)} className="btn btn--secondary">
              Voltar à primeira página
            </Link>
          </div>
        )}

        {historico.data.length > 0 && (
          <ul className="m-0 flex list-none flex-col divide-y divide-line p-0">
            {historico.data.map((evento) => (
              <li key={evento.id} className="grid gap-1.5 py-3 first:pt-0 last:pb-0">
                <div className="flex flex-wrap items-center gap-2">
                  {/* O selo sai de `habilitado_para` — o estado para o qual o
                      canal foi movido —, e não da diferença entre `de` e
                      `para`: é o que continua correto se a API passar a gravar
                      evento sem mudança. Mesma variante do selo do canal, para
                      "Desligado" não significar duas coisas na mesma tela. */}
                  <span
                    className={`state ${evento.habilitado_para ? "state--1" : "state--off"}`}
                  >
                    {evento.habilitado_para ? "Ligado" : "Desligado"}
                  </span>
                  <strong className="text-sm text-ink">{rotuloDoCanal(evento.canal)}</strong>
                  <span className="text-xs text-muted">
                    {formatarDataHora(evento.created_at)}
                  </span>
                </div>

                <p className="m-0 text-xs text-muted">
                  {/* A chave de integração não é pessoa, e a API não forja uma:
                      mostrá-la como o e-mail que ela não é faria o histórico
                      anunciar alguém chamado "api" desligando o canal. */}
                  por{" "}
                  <span className="text-ink">
                    {ehAtorMaquina(evento.usuario)
                      ? "chave de integração (X-API-Key), não uma pessoa"
                      : evento.usuario}
                  </span>
                </p>
              </li>
            ))}
          </ul>
        )}

        {total > LIMITE_HISTORICO && historico.data.length > 0 && (
          <nav
            aria-label="Paginação do histórico"
            className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4"
          >
            <p className="m-0 text-xs text-muted">
              Mostrando {primeiroDaPagina}–{ultimoDaPagina} de {total}
            </p>
            <div className="flex gap-2">
              {offset > 0 ? (
                <Link
                  href={urlDoHistorico(Math.max(0, offset - LIMITE_HISTORICO))}
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
                  href={urlDoHistorico(offset + LIMITE_HISTORICO)}
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
