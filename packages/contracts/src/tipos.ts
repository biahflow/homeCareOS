/**
 * Tipos do contrato HTTP entre o frontend e a API do HomeCareOS.
 *
 * Este pacote não depende de React: existe para que qualquer cliente (web,
 * futuro app do técnico em React Native) reutilize os mesmos tipos e o mesmo
 * cliente HTTP sem arrastar a UI web junto.
 */

export type DocumentoStatus =
  | "processando"
  | "aprovado"
  | "problema"
  | "incompleto"
  | "em_correcao"
  | "resolvido"
  | "liberado";

export interface DocumentoCriado {
  id: string;
  pagina: number;
  status: DocumentoStatus;
  /** Competência no formato "YYYY-MM". */
  competencia: string;
}

export interface UploadResponse {
  documentos: DocumentoCriado[];
}

export interface UploadParams {
  arquivo: File;
  /** Competência no formato "YYYY-MM". Não é extraível do documento. */
  competencia: string;
  /** UUID gerado no cliente, enviado no header `Idempotency-Key`. */
  idempotencyKey: string;
}

/* Paginação — envelope comum a toda listagem da API. */

/**
 * Quanto a listagem devolveu e de onde. `total` é o tamanho do conjunto
 * **filtrado**, não o da tabela: é ele que diz quantas páginas existem para os
 * filtros em vigor.
 */
export interface Paginacao {
  total: number;
  limite: number;
  offset: number;
}

/**
 * Envelope de toda listagem paginada (`apps/api/src/homecareos/api/pagination.py`).
 *
 * Genérico de propósito: o formato é um só para documentos, pendências e
 * pacientes, e repeti-lo por endpoint criaria cópias para divergir.
 *
 * Paginação é por **deslocamento** (`offset`), não por cursor. A consequência
 * que quem consome precisa saber: entre duas páginas o conjunto pode mudar
 * debaixo de quem lê — um item novo empurra os demais para frente e a página
 * seguinte pode repetir ou pular uma linha. É aceitável aqui (listas filtradas
 * e ordenadas por coluna simples) e não é defeito da interface.
 */
export interface RespostaPaginada<T> {
  data: T[];
  paginacao: Paginacao;
}

/* Pendências — `/api/pendencias`. */

/**
 * Ciclo de vida de uma pendência: `aberta → em_correcao → resolvida`.
 *
 * **Sempre para frente, nunca pulando etapa nem voltando**
 * (`routers/pendencias.py:_TRANSICOES_VALIDAS`). `resolvida` não transiciona
 * para lugar nenhum: não existe caminho de volta pela API, e desfazer é
 * trabalho de quem administra o banco.
 */
export type PendenciaStatus = "aberta" | "em_correcao" | "resolvida";

/**
 * Uma pendência aberta sobre um documento durante a conferência.
 *
 * `descricao` e `tipo_problema` descrevem o problema de uma evolução de
 * prontuário — são dado clínico. Não vão para `console.log`, para query string
 * nem para mensagem de erro que suba para telemetria.
 */
export interface PendenciaItem {
  id: string;
  documento_id: string;
  tipo_problema: string;
  /** Campo do schema de extração que originou a pendência. Nulo em pendência anterior à classificação automática. */
  campo: string | null;
  descricao: string;
  /** Rótulo legível: pessoa, setor ou fornecedor. O padrão da classificação automática é texto livre. */
  responsavel: string;
  /** Nulo enquanto a pendência não foi atribuída a uma pessoa cadastrada — o caso de toda pendência aberta pela classificação. */
  responsavel_id: string | null;
  status: PendenciaStatus;
  /** ISO 8601 com fuso (a API grava `timestamptz`). */
  deadline: string;
  created_at: string;
  /** ISO 8601, ou nulo enquanto a pendência não foi resolvida. */
  resolved_at: string | null;
}

/** Filtros de `GET /api/pendencias`. Nenhum deles carrega dado de prontuário. */
export interface ListarPendenciasParams {
  status?: PendenciaStatus;
  /** Data `AAAA-MM-DD`: só pendências com deadline até ela, **dia inteiro incluído**. */
  deadline?: string;
  /** Operadora do documento a que a pendência pertence. */
  operadora_id?: string;
  /** Itens por página. Padrão 50, máximo 200 (`api/pagination.py`). */
  limite?: number;
  offset?: number;
}

/**
 * Corpo de `PATCH /api/pendencias/{id}`.
 *
 * `responsavel_id` existe na API e **não** está aqui de propósito: não há
 * router de usuários, então não existe de onde listar pessoas para escolher, e
 * um id chutado responde 422. A atribuição desta interface usa o texto livre
 * `responsavel`, que é o que a operação já faz — ela atribui a fornecedor, a
 * setor e a gente que ainda não tem cadastro. Quando existir cadastro de
 * usuário via API (débito conhecido da issue #39), este tipo ganha o campo.
 *
 * `responsavel` ausente significa **não mexer no responsável atual**; não é o
 * mesmo que enviá-lo vazio, que sobrescreveria o rótulo por uma string vazia.
 */
export interface AtualizarPendenciaParams {
  status: PendenciaStatus;
  responsavel?: string;
}

/** Faixas de vencimento do resumo. Contam apenas pendências **não resolvidas**. */
export interface FaixasDeDeadline {
  vencidas: number;
  proximos_7_dias: number;
  futuras: number;
}

/**
 * Resposta de `GET /api/pendencias/resumo`.
 *
 * `por_status` conta **todas** as pendências, inclusive as resolvidas;
 * `por_faixa_deadline` conta só as que continuam em aberto (`routers/
 * pendencias.py:resumo_pendencias`). Os dois totais não fecham entre si, e
 * apresentá-los como se fechassem faria alguém procurar um erro que não existe.
 */
export interface ResumoPendencias {
  por_status: Record<PendenciaStatus, number>;
  por_faixa_deadline: FaixasDeDeadline;
}

/* Operadoras — `/api/operadoras`. */

/** Operadora (convênio) atendida pela empresa. Lista pequena, sem paginação. */
export interface Operadora {
  id: string;
  nome: string;
  codigo: string;
  created_at: string;
}

/* Autenticação — `/api/auth` (ADR 0001). */

/** Os três papéis da matriz aprovada no ADR 0001. */
export type Papel = "conferente" | "coordenador" | "gestor";

/** Usuário como a API o devolve. Não existe campo de senha aqui. */
export interface UsuarioOut {
  id: string;
  nome: string;
  email: string;
  papel: Papel;
  ativo: boolean;
}

/**
 * Resposta de `GET /api/auth/eu` para a integração máquina-a-máquina
 * (`X-API-Key`): não há pessoa por trás da chave, e a API não forja uma.
 */
export interface MaquinaOut {
  tipo: "maquina";
}

/**
 * Resposta de `POST /api/auth/login` quando a conta tem MFA ativado.
 *
 * **Vazia de dado do usuário de propósito** — quem apresentou só a senha ainda
 * não provou quem é. É por isso que a tela de MFA não tem nome, papel nem
 * e-mail para mostrar, e não deve inventar nenhum.
 */
export interface MfaPendenteOut {
  mfa_pendente: true;
}

export interface LoginParams {
  email: string;
  senha: string;
}

/* Recuperação de senha (issue #34) — `/api/auth/senha/*`. */

export interface EsqueciSenhaParams {
  email: string;
}

export interface RedefinirSenhaParams {
  /** Token de uso único recebido por e-mail. Não confundir com o cookie de sessão. */
  token: string;
  nova_senha: string;
}

export interface MfaVerificarParams {
  /**
   * Os seis dígitos do app autenticador **ou** um código de recuperação
   * (`a1b2c-3d4e5`). Um campo só, como a API exige: dois campos separados
   * diriam a quem sonda qual dos dois caminhos falhou.
   */
  codigo: string;
}

/**
 * Resposta de `POST /api/auth/mfa/iniciar`: o segredo TOTP recém-gravado.
 *
 * **Os dois campos são a credencial**, e `otpauth_uri` carrega o `secret`
 * dentro dele. Vale para os dois o que vale para uma senha: não vão para
 * `console.log`, não entram em query string (histórico, log de proxy e header
 * `Referer` guardariam o segredo), não são persistidos em lugar nenhum do
 * cliente e não viajam em payload de Server Component. Vivem em memória
 * enquanto a tela de cadastro está aberta, e acabam com ela.
 */
export interface MfaIniciarOut {
  /** Segredo em base32 — o que se digita à mão em app que não lê QR code. */
  secret: string;
  /** O mesmo segredo no formato `otpauth://totp/...`, que vira o QR code. */
  otpauth_uri: string;
}

export interface MfaConfirmarParams {
  /** Os seis dígitos que o app autenticador mostra para o segredo cadastrado. */
  codigo: string;
}

/**
 * Resposta de `POST /api/auth/mfa/confirmar`: os códigos de recuperação em
 * claro, e **esta é a única vez que eles existem**.
 *
 * O banco guarda só o hash Argon2id (`db/models/codigo_recuperacao_mfa.py`) e
 * não há endpoint que os mostre de novo. Quem consome isto tem uma obrigação
 * que o tipo não consegue expressar: dar à pessoa a chance de guardá-los antes
 * de sair da tela, e não persisti-los no cliente para "facilitar depois" —
 * armazená-los desfaz o motivo de a API só guardar o hash.
 */
export interface MfaCodigosRecuperacaoOut {
  codigos: string[];
}

export interface MfaDesativarParams {
  /**
   * Os **dois** fatores, porque a API exige os dois: com só o código, uma
   * sessão sequestrada desligaria sozinha o segundo fator; com só a senha,
   * bastaria a senha vazada — que é a hipótese que faz alguém ativar MFA.
   */
  senha: string;
  codigo: string;
}

export type LoginResposta = UsuarioOut | MfaPendenteOut;
export type EuResposta = UsuarioOut | MaquinaOut;

/**
 * Distingue o usuário das duas respostas que **não** carregam usuário nenhum.
 *
 * O teste é a presença do `id`, e não a de um discriminador, porque é isso que
 * `MfaPendenteOut` e `MaquinaOut` têm em comum: as duas são deliberadamente
 * vazias de dado da pessoa. Enquanto este guard for falso, não há nada do
 * usuário para a interface desenhar.
 */
export function ehUsuario(resposta: LoginResposta | EuResposta): resposta is UsuarioOut {
  return "id" in resposta;
}
