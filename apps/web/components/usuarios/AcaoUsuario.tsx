"use client";

import { useRouter } from "next/navigation";
import { useId, useState, useTransition } from "react";
import { ApiError, atualizarUsuario, detalhesDeValidacao } from "@homecareos/contracts";
import type { AtualizarUsuarioParams, Papel } from "@homecareos/contracts";
import { API_BASE_URL } from "@/lib/env";
import { PAPEL_LABEL } from "@/components/shell/usuario";
import { PAPEIS_ATRIBUIVEIS } from "./filtros";

const MENSAGEM_NOME_VAZIO = "O nome não pode ficar em branco.";
const MENSAGEM_SEM_MUDANCA = "Nada mudou nos campos acima.";
const MENSAGEM_SUMICO =
  "Este usuário não existe mais na API. Nada foi alterado; recarregue a página para ver o cadastro atual.";
const MENSAGEM_INESPERADA = "Falha inesperada ao alterar o usuário. Tente novamente.";

/**
 * O 422 é o único que não tem a frase útil em `message` — a API responde
 * "parâmetros inválidos" fixo e põe o que a regra disse em `detalhes`. As
 * recusas que importam nesta tela (403 de papel não atribuível, 403 de
 * auto-serviço, 409 do último coordenador) já chegam com a frase pronta, e cada
 * uma **diz o que fazer a respeito**: pedir a outro coordenador, promover
 * alguém antes, usar a linha de comando. Reescrevê-las aqui trocaria a
 * instrução por um "não deu".
 */
function mensagemDoErro(erro: unknown): string {
  if (!(erro instanceof ApiError)) {
    return MENSAGEM_INESPERADA;
  }
  if (erro.status === 422) {
    const detalhes = detalhesDeValidacao(erro);
    return detalhes.length > 0 ? detalhes.join(" ") : erro.message;
  }
  return erro.message;
}

type Estado =
  | { tipo: "fechado" }
  | { tipo: "editando" }
  | { tipo: "confirmando"; mudanca: AtualizarUsuarioParams }
  | { tipo: "erro"; mensagem: string };

/**
 * Alterar nome, papel e situação de um usuário.
 *
 * **Não há exclusão, e não é omissão**: `log_conferencia.usuario_id` aponta para
 * `usuarios`, e apagar uma pessoa apagaria a resposta a "quem fez esta ação?".
 * A API não tem `DELETE` nesta rota. Desativar é o que existe.
 *
 * **Na própria linha não há controle de papel nem de situação.** As duas são
 * recusadas pela API com 403 (`não é possível alterar o próprio papel` e `não é
 * possível desativar a própria conta`), e oferecer um controle que só existe
 * para ser recusado faz a pessoa descobrir a regra pelo erro. Isto não
 * substitui o tratamento do 403: aba antiga, outra sessão e papel alterado no
 * servidor continuam produzindo a recusa.
 *
 * **Desativar pede confirmação; renomear e trocar papel não.** A assimetria é
 * deliberada: só a desativação derruba a pessoa do sistema na hora — ela revoga
 * todas as sessões abertas na mesma transação —, e confirmar cada passo treina
 * a pessoa a clicar em "sim" sem ler, justamente o que estraga a confirmação
 * que importa.
 */
export function AcaoUsuario({
  usuarioId,
  nome,
  papel,
  ativo,
  ehVoce,
}: {
  usuarioId: string;
  nome: string;
  papel: Papel;
  ativo: boolean;
  /** É a linha de quem está logado? Ver a docstring do componente. */
  ehVoce: boolean;
}) {
  const router = useRouter();
  const nomeId = useId();
  const papelId = useId();
  const situacaoId = useId();
  const [estado, setEstado] = useState<Estado>({ tipo: "fechado" });
  const [nomeEditado, setNomeEditado] = useState(nome);
  const [papelEditado, setPapelEditado] = useState<Papel>(papel);
  const [ativoEditado, setAtivoEditado] = useState(ativo);
  const [enviando, setEnviando] = useState(false);
  const [recarregando, iniciarRecarga] = useTransition();

  const ocupado = enviando || recarregando;

  function abrir() {
    // Os campos partem sempre do que o servidor acabou de mandar, e não do que
    // sobrou de uma edição anterior: entre uma abertura e outra a lista pode ter
    // sido recarregada com outro valor.
    setNomeEditado(nome);
    setPapelEditado(papel);
    setAtivoEditado(ativo);
    setEstado({ tipo: "editando" });
  }

  function fechar() {
    setEstado({ tipo: "fechado" });
  }

  function recarregarLista() {
    iniciarRecarga(() => {
      router.refresh();
    });
  }

  /**
   * Só o que mudou entra no corpo: campo omitido é campo não alterado, e mandar
   * de volta o valor que já está lá amplia sem motivo o que a requisição pode
   * recusar — `papel` reenviado igual é o caminho para um 403 de "próprio
   * papel" numa edição que só queria corrigir a grafia de um nome.
   */
  function mudancaPedida(): AtualizarUsuarioParams {
    const mudanca: AtualizarUsuarioParams = {};
    const nomeLimpo = nomeEditado.trim();
    if (nomeLimpo !== nome) mudanca.nome = nomeLimpo;
    if (!ehVoce && papelEditado !== papel) mudanca.papel = papelEditado;
    if (!ehVoce && ativoEditado !== ativo) mudanca.ativo = ativoEditado;
    return mudanca;
  }

  async function salvar(mudanca: AtualizarUsuarioParams) {
    setEnviando(true);
    try {
      await atualizarUsuario(API_BASE_URL, usuarioId, mudanca);
      setEnviando(false);
      setEstado({ tipo: "fechado" });
      // A resposta traz o usuário alterado, mas não o efeito colateral: uma
      // desativação revogou as sessões dela na mesma transação. Quem tem o
      // retrato certo é o servidor.
      recarregarLista();
    } catch (causa) {
      if (causa instanceof ApiError && causa.status === 401) {
        // A sessão acabou no servidor — mesmo tratamento do resto da área
        // logada: voltar ao login dizendo o que houve. `enviando` continua
        // ligado de propósito, a navegação já está em curso.
        router.replace("/login?motivo=sessao-encerrada");
        router.refresh();
        return;
      }

      setEnviando(false);

      if (causa instanceof ApiError && causa.status === 404) {
        // **Sem `router.refresh()` aqui, de propósito.** A fila de pendências
        // recarrega no caso análogo, mas ela tem `AvisosDaFila` para a mensagem
        // sobreviver à linha que sai da lista; recarregar daqui desmontaria
        // este componente e levaria junto a única explicação que a pessoa
        // receberia. Ela lê o que houve e recarrega quando quiser.
        //
        // Vale notar que este 404 é quase inalcançável: não existe exclusão de
        // usuário em lugar nenhum do sistema (a auditoria referencia a conta, e
        // por isso a API não tem `DELETE`), então chegar aqui exige alguém
        // apagando a linha direto no banco.
        setEstado({ tipo: "erro", mensagem: MENSAGEM_SUMICO });
        return;
      }

      // 403 e 409 mantêm o formulário aberto, ao contrário do que a fila de
      // pendências faz. Lá o 403 é do papel de quem clicou e insistir nunca
      // muda nada, então o controle sai de cena. Aqui a tela inteira já é do
      // coordenador, e o que a API recusa é **este pedido**: um papel que ela
      // não atribui (outro papel passa) ou uma alteração que esvaziaria a
      // coordenação (promover alguém antes resolve). Sumir com o controle
      // obrigaria a recarregar a página para tentar a correção que a própria
      // mensagem acabou de ensinar.
      setEstado({ tipo: "erro", mensagem: mensagemDoErro(causa) });
    }
  }

  function tentarSalvar() {
    // A recusa anterior sai da tela antes da nova tentativa: mantê-la enquanto
    // o pedido corrigido está no ar faria a pessoa ler a mensagem antiga como
    // resposta ao que ela acabou de mandar.
    setEstado({ tipo: "editando" });
    if (nomeEditado.trim() === "") {
      setEstado({ tipo: "erro", mensagem: MENSAGEM_NOME_VAZIO });
      return;
    }
    const mudanca = mudancaPedida();
    if (Object.keys(mudanca).length === 0) {
      setEstado({ tipo: "erro", mensagem: MENSAGEM_SEM_MUDANCA });
      return;
    }
    if (mudanca.ativo === false) {
      setEstado({ tipo: "confirmando", mudanca });
      return;
    }
    void salvar(mudanca);
  }

  if (estado.tipo === "fechado") {
    return (
      <button
        type="button"
        className="btn btn--secondary h-9 min-h-9 px-3 text-xs"
        disabled={ocupado}
        onClick={abrir}
      >
        {recarregando ? "Atualizando…" : "Editar"}
      </button>
    );
  }

  if (estado.tipo === "confirmando") {
    return (
      <div className="grid gap-2 rounded-xl border border-red-200 bg-red-50 p-3 sm:w-72">
        <p className="m-0 text-xs leading-5 text-red-700">
          Desativar <strong>{nome}</strong>? A conta perde o acesso e{" "}
          <strong>todas as sessões abertas dela são encerradas na hora</strong> — inclusive a do
          navegador em que ela estiver trabalhando agora.
        </p>
        <p className="m-0 text-xs leading-5 text-red-700">
          A conta não é apagada: ela continua respondendo por quem fez o quê no histórico, e pode
          ser reativada aqui depois.
        </p>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="btn btn--primary h-9 min-h-9 px-3 text-xs"
            disabled={ocupado}
            onClick={() => void salvar(estado.mudanca)}
          >
            {ocupado ? "Desativando…" : "Confirmar desativação"}
          </button>
          <button
            type="button"
            className="btn btn--secondary h-9 min-h-9 px-3 text-xs"
            disabled={ocupado}
            onClick={() => setEstado({ tipo: "editando" })}
          >
            Cancelar
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="grid gap-2 rounded-xl border border-line bg-canvas p-3 sm:w-72">
      <div className="grid gap-1">
        <label htmlFor={nomeId} className="text-[11px] font-semibold text-muted">
          Nome
        </label>
        <input
          id={nomeId}
          type="text"
          className="field px-3 py-2 text-xs"
          value={nomeEditado}
          disabled={ocupado}
          onChange={(evento) => setNomeEditado(evento.target.value)}
        />
      </div>

      {ehVoce ? (
        <p className="m-0 text-[11px] leading-4 text-muted">
          Esta é a sua conta: o sistema não deixa ninguém alterar o próprio papel nem se desativar.
          Peça a outro coordenador.
        </p>
      ) : (
        <>
          <div className="grid gap-1">
            <label htmlFor={papelId} className="text-[11px] font-semibold text-muted">
              Papel
            </label>
            <select
              id={papelId}
              className="field px-3 py-2 text-xs"
              value={papelEditado}
              disabled={ocupado}
              onChange={(evento) => setPapelEditado(evento.target.value as Papel)}
            >
              {/* O papel atual entra na lista mesmo quando não é atribuível —
                  um gestor cadastrado por linha de comando aparece nesta tela, e
                  sem a própria opção o controle mostraria outro papel e a
                  pessoa salvaria um rebaixamento que não pediu. */}
              {(PAPEIS_ATRIBUIVEIS.includes(papel)
                ? PAPEIS_ATRIBUIVEIS
                : [...PAPEIS_ATRIBUIVEIS, papel]
              ).map((opcao) => (
                <option key={opcao} value={opcao}>
                  {PAPEL_LABEL[opcao]}
                </option>
              ))}
            </select>
          </div>

          <div className="grid gap-1">
            <label htmlFor={situacaoId} className="text-[11px] font-semibold text-muted">
              Situação
            </label>
            <select
              id={situacaoId}
              className="field px-3 py-2 text-xs"
              value={String(ativoEditado)}
              disabled={ocupado}
              onChange={(evento) => setAtivoEditado(evento.target.value === "true")}
            >
              <option value="true">Ativo</option>
              <option value="false">Desativado</option>
            </select>
          </div>
        </>
      )}

      <p className="m-0 text-[11px] leading-4 text-muted">
        E-mail e senha não se alteram por aqui: o e-mail é a credencial de acesso da pessoa, e a
        senha só ela define.
      </p>

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className="btn btn--primary h-9 min-h-9 px-3 text-xs"
          disabled={ocupado}
          onClick={tentarSalvar}
        >
          {ocupado ? "Salvando…" : "Salvar"}
        </button>
        <button
          type="button"
          className="btn btn--secondary h-9 min-h-9 px-3 text-xs"
          disabled={ocupado}
          onClick={fechar}
        >
          Cancelar
        </button>
      </div>

      {/* A frase sai como a API a escreveu, minúscula inicial inclusive — é o
          que o resto do app faz com mensagem de erro da API, e é ela que diz o
          que fazer. */}
      {estado.tipo === "erro" && (
        <p role="alert" className="alert--error text-xs">
          {estado.mensagem}
        </p>
      )}
    </div>
  );
}
