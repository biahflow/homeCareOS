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

/* Documentos em conferência — listagem e detalhe de `/api/documentos`. */

/**
 * Um documento na listagem — **sem** extração e sem validações.
 *
 * Elas existem só no detalhe ({@link DocumentoDetalhe}), e isso é decisão da
 * API: cada extração carrega o prontuário inteiro em `campos_extraidos`, e uma
 * página de 50 documentos traria 50 prontuários para uma tela que mostra status
 * e competência.
 *
 * `tipo` usa {@link TipoDocumento}, declarado adiante junto dos tipos de
 * relatório: é o mesmo enum do banco (`db/models/enums.py`), não uma segunda
 * lista.
 */
export interface DocumentoListItem {
  id: string;
  tipo: TipoDocumento;
  /** Competência no formato "AAAA-MM". */
  competencia: string;
  status: DocumentoStatus;
  /**
   * Página de origem quando o documento veio de um PDF multi-página — um PDF de
   * dez páginas vira dez documentos. **Nula** quando não há página de origem
   * (`documentos.pagina` é nullable no banco), e nulo aqui não é "página zero".
   */
  pagina: number | null;
  paciente_id: string | null;
  operadora_id: string | null;
  /** ISO 8601 com fuso (a API grava `timestamptz`). */
  created_at: string;
  updated_at: string;
}

/**
 * A última extração do documento, como a API a resume.
 *
 * `campos_extraidos` e `confianca_por_campo` são `dict[str, Any]` na API — não
 * há schema fechado no contrato HTTP, porque o schema de extração
 * (`extraction/schema.py:EvolucaoProntuario`) evolui sem versionar esta rota.
 * Por isso `unknown` e não `any`: quem exibe é **obrigado** a checar o tipo de
 * cada valor antes de usá-lo. Um `any` aqui devolveria a garantia ao acaso, e
 * um campo novo com formato inesperado derrubaria a tela que existe justamente
 * para mostrar que a extração saiu ruim.
 */
export interface ExtracaoResumo {
  id: string;
  /** Os campos que o provider de Vision leu. Valor `null` é "não leu", não "vazio". */
  campos_extraidos: Record<string, unknown>;
  /**
   * Confiança agregada, 0..1 — a **média simples** da confiança por campo
   * (`extraction/claude.py:_confianca`). Não é percentual, e não é qualidade do
   * documento: é o quanto o próprio modelo declarou ter lido.
   */
  confianca: number;
  /**
   * Confiança por campo, 0..1, em três níveis que o provider produz: `0.0` para
   * campo que ele listou como ilegível, `0.5` para campo lido com dúvida
   * (`campos_incertos`) e `1.0` para o resto. **A faixa do meio é a que importa
   * ao conferente** — é o campo que foi lido, mas vale conferir contra o papel.
   *
   * Os valores chegam como `unknown` porque a API os tipa como `Any`; quem
   * exibe checa se é número antes de tratá-lo como um.
   */
  confianca_por_campo: Record<string, unknown>;
  /** Vazio quando nenhum provider real rodou (ver `provider`). */
  modelo: string;
  /**
   * Provider que extraiu. O literal `"null"` é o `NullExtractionProvider`: sem
   * `ANTHROPIC_API_KEY` configurada a extração não roda, todo campo entra como
   * ilegível e a confiança sai `0.0`. É configuração, não falha — e a tela
   * precisa poder dizer isso em vez de apresentar zeros como se fossem leitura.
   */
  provider: string;
  created_at: string;
}

/** Resultado da aplicação de uma regra de operadora sobre um documento. */
export type ResultadoValidacao = "aprovado" | "reprovado";

/**
 * Uma regra aplicada ao documento e o que ela decidiu.
 *
 * `detalhe` é o texto que a validação escreveu ("Campo 'carimbo_legivel' foi
 * marcado como ilegível pela extração") — é ele que se mostra, não `regra_id`.
 * O id da regra é referência técnica: **não existe endpoint que o resolva para
 * o público desta tela** (`GET /api/regras` exige coordenador), então traduzi-lo
 * para um nome quebraria a tela da conferente com 403.
 *
 * A API devolve as validações **sem ordenar** (`obter_documento` não tem
 * `order_by`): a ordem é a que o Postgres devolver, e quem exibe ordena por
 * `created_at` se a ordem importar.
 */
export interface ValidacaoResumo {
  id: string;
  regra_id: string;
  resultado: ResultadoValidacao;
  detalhe: string;
  created_at: string;
}

/**
 * O documento com tudo o que a conferência precisa, incluindo o que descreve
 * o arquivo — mas não os bytes dele.
 *
 * **`arquivo_url` não é uma URL.** Apesar do nome, o campo guarda a *chave do
 * objeto no storage* (`documentos/{id}/{sha256}.png`, montada em
 * `intake/service.py`). Usá-la como `src` de imagem ou `href` de link produz
 * um caminho relativo quebrado; montar a URL do MinIO no cliente também não
 * funciona (rede interna do Compose, e sem credencial). Para exibir a página
 * escaneada, use {@link caminhoArquivoDocumento} — `GET
 * /api/documentos/{id}/arquivo` (issue #51, PR #54) serve o arquivo em
 * streaming, e é esse endpoint, não este campo, que carrega os bytes. Trate
 * `arquivo_url` como referência técnica para achar o objeto no storage.
 */
export interface DocumentoDetalhe extends DocumentoListItem {
  /** **Chave do storage, não endereço.** Ver a nota da interface. */
  arquivo_url: string;
  /** `null` enquanto nenhuma extração foi registrada para o documento. */
  extracao: ExtracaoResumo | null;
  validacoes: ValidacaoResumo[];
}

/** Filtros de `GET /api/documentos`. Nenhum deles carrega dado de prontuário. */
export interface ListarDocumentosParams {
  /** Competência "AAAA-MM". A API compara como texto, sem normalizar. */
  competencia?: string;
  status?: DocumentoStatus;
  operadora_id?: string;
  paciente_id?: string;
  /** Itens por página. Padrão 50, máximo 200 (`api/pagination.py`). */
  limite?: number;
  offset?: number;
}

/**
 * Resposta de `POST /api/documentos/{id}/revalidar`: onde o documento parou e
 * quanto ainda falta.
 *
 * `status` é o status **depois** da reclassificação, e revalidar pode piorá-lo:
 * de `resolvido` o documento volta para `problema`/`incompleto` quando as
 * regras reprovam de novo (`db/models/enums.py:DocumentoStatus`). Não é
 * confirmação de que deu certo — é o resultado, seja ele qual for.
 */
export interface RevalidacaoResponse {
  documento_id: string;
  status: DocumentoStatus;
  /** Pendências não resolvidas do documento depois da revalidação. */
  pendencias_abertas: number;
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

/* Relatórios e métricas — `/api/relatorios` (issue #8). */

/** Tipo do documento ingerido (`db/models/enums.py:TipoDocumento`). */
export type TipoDocumento = "evolucao" | "ficha_visita" | "boletim" | "matmed";

/**
 * Gravidade da linha no painel de conferência, **decidida pela API**.
 *
 * Ela existe exatamente para o cliente não reimplementar a regra "aprovado
 * verde, problema amarelo, incompleto vermelho": o mapeamento status →
 * severidade é decisão de produto e mora em `reports/conferencia.severidade_de`.
 * Quem consome traduz **severidade** em estilo; derivar a cor do `status`
 * recriaria a regra num segundo lugar, para divergir dela na primeira mudança.
 */
export type Severidade = "CRITICO" | "ATENCAO" | "OK";

/**
 * Uma linha do relatório de conferência: um documento da competência, com o
 * problema encontrado e a ação necessária já resolvidos pela API.
 *
 * `paciente_nome` é **dado de paciente**. Vale para ele o que vale para a
 * descrição de uma pendência: não vai para `console.log`, para query string nem
 * para mensagem de erro que suba para telemetria. O CSV do mesmo relatório
 * carrega a mesma coluna e fica salvo na máquina de quem baixou.
 */
export interface LinhaConferencia {
  documento_id: string;
  tipo: TipoDocumento;
  /** Competência no formato "AAAA-MM". */
  competencia: string;
  status: DocumentoStatus;
  severidade: Severidade;
  /** ISO 8601 com fuso (a API grava `timestamptz`). */
  recebido_em: string;
  /**
   * `AAAA-MM-DD` lido da última extração, ou nulo quando não há extração ainda
   * ou o campo veio ilegível — o relatório do dia não cai por extração ruim.
   */
  data_atendimento: string | null;
  paciente_id: string | null;
  paciente_nome: string | null;
  operadora_id: string | null;
  operadora_nome: string | null;
  pendencias_abertas: number;
  /**
   * Descrições das pendências **não resolvidas** unidas por `" | "`, e `""`
   * quando não há nenhuma aberta. String vazia é "nenhum problema em aberto",
   * não "problema desconhecido".
   */
  problema_encontrado: string;
  acao_necessaria: string;
  /** Menor deadline entre as pendências não resolvidas, ou nulo quando não há. */
  deadline: string | null;
}

/**
 * Os filtros do relatório de conferência — os mesmos para o JSON paginado e
 * para o CSV (`filtro_conferencia` é uma dependency compartilhada na API).
 *
 * Nenhum deles carrega dado de prontuário: `paciente_id` é identificador, e o
 * nome do paciente nunca vira filtro de URL.
 */
export interface FiltrosConferencia {
  /** Competência "AAAA-MM". */
  competencia?: string;
  status?: DocumentoStatus;
  operadora_id?: string;
  paciente_id?: string;
  /** Data "AAAA-MM-DD": recebidos a partir dela, inclusive. */
  data_inicio?: string;
  /** Data "AAAA-MM-DD": recebidos até ela, **dia inteiro incluído**. */
  data_fim?: string;
  /** Só documentos com pendência não resolvida. */
  apenas_pendentes?: boolean;
}

export interface RelatorioConferenciaParams extends FiltrosConferencia {
  /** Itens por página. Padrão 50, máximo 200 (`api/pagination.py`). */
  limite?: number;
  offset?: number;
}

/**
 * O que a **conferência mediu**: pendência detectada antes do envio à operadora.
 *
 * Nunca se funde com {@link MetricasGlosaInformada}. Ver
 * {@link MetricasCompetencia}.
 */
export interface MetricasSistema {
  documentos: number;
  /**
   * Contagem por status **atual** do documento. É foto do agora, não histórico:
   * um documento que teve problema, foi corrigido e virou `liberado` aparece
   * como `liberado`. Por isso ele sozinho não serve de indicador de qualidade —
   * quanto melhor a correção funciona, melhor a foto fica.
   */
  por_status: Record<string, number>;
  /** Documentos com ao menos uma pendência, **em qualquer status**, inclusive resolvidas. */
  documentos_com_pendencia: number;
  /** Razão 0..1 (não percentual), arredondada em 4 casas pela API. */
  taxa_documentos_com_pendencia: number;
  pendencias_abertas: number;
  pendencias_vencidas: number;
  pendencias_proximos_7_dias: number;
  /** Nulo quando nenhuma pendência foi resolvida ainda: zero seria "resolvem instantaneamente". */
  tempo_medio_resolucao_horas: number | null;
}

/**
 * O que foi **informado à mão**: glosa, o que a operadora recusou depois do
 * envio, digitado de um demonstrativo.
 *
 * `fonte` é obrigatório na API justamente para este bloco poder dizer, na tela,
 * de onde o número veio.
 */
export interface MetricasGlosaInformada {
  documentos_enviados: number;
  documentos_glosados: number;
  /** Razão 0..1 (não percentual), arredondada em 4 casas pela API. */
  taxa_glosa: number;
  /** **Inteiro em centavos**, nunca reais. Nulo quando não informado. */
  valor_glosado_centavos: number | null;
  horas_conferencia: number | null;
  fonte: string;
}

/**
 * Os dois blocos de uma competência, **lado a lado e nomeados — nunca fundidos**.
 *
 * `sistema` mede o que a conferência pegou **antes** do envio; `glosa_informada`
 * mede o que a operadora recusou **depois**, digitado de um demonstrativo. São
 * medidas de origens diferentes: somá-las, dividir uma pela outra ou apresentá-las
 * num indicador único de "eficácia" inventa uma relação que o dado não sustenta —
 * e é decisão de produto que ninguém tomou.
 *
 * `glosa_informada` é `null` quando **não há baseline registrado** para a
 * competência. `null` significa "ninguém informou", **nunca zero**: renderizar
 * 0% de glosa onde não há baseline afirmaria que a conferência zerou a glosa —
 * exatamente o número que justifica o produto, inventado.
 */
export interface MetricasCompetencia {
  /** Competência "AAAA-MM". */
  competencia: string;
  sistema: MetricasSistema;
  glosa_informada: MetricasGlosaInformada | null;
}

/** Quanto trabalho cada operadora dá na janela pedida. */
export interface MetricasOperadora {
  /** Nulo agrupa os documentos que ninguém conseguiu vincular a uma operadora. */
  operadora_id: string | null;
  nome: string;
  documentos: number;
  documentos_com_pendencia: number;
  /** Razão 0..1 (não percentual). */
  taxa_documentos_com_pendencia: number;
}

/** Documentos recebidos por dia, com a fronteira do dia fixada em UTC pela API. */
export interface VolumeDia {
  /** Data "AAAA-MM-DD". */
  data: string;
  documentos: number;
}

/** Antes/depois honesto: a **mesma** medida (glosa informada) nas duas pontas. */
export interface ComparacaoGlosa {
  competencia_inicial: string;
  competencia_final: string;
  /** Razão 0..1 (não percentual). */
  taxa_glosa_inicial: number;
  taxa_glosa_final: number;
  /** `(final - inicial) * 100`. Queda de glosa é negativa. */
  variacao_pontos_percentuais: number;
}

export interface MetricasResponse {
  /** Em ordem **crescente** de competência, como a API as devolve. */
  competencias: MetricasCompetencia[];
  por_operadora: MetricasOperadora[];
  por_dia: VolumeDia[];
  /**
   * Nulo enquanto menos de duas competências da janela tiverem baseline: não há
   * como comparar contra uma ponta que não existe, e inventá-la (com zero, ou
   * com a média das demais) transformaria o indicador numa ficção.
   */
  comparacao_glosa: ComparacaoGlosa | null;
}

export interface MetricasParams {
  /** Competência "AAAA-MM". Sem janela, a API devolve as 12 mais recentes. */
  competencia_inicio?: string;
  competencia_fim?: string;
  operadora_id?: string;
}

/**
 * Corpo de `PUT /api/relatorios/baseline` — dado digitado de um demonstrativo
 * da operadora.
 *
 * Duas armadilhas do contrato:
 *
 * - **`valor_glosado_centavos` é inteiro em centavos**, nunca reais. Quem
 *   preenche a partir de um campo em reais converte multiplicando por 100 com
 *   aritmética inteira; um `float` no caminho erra por centavo, e uma conversão
 *   esquecida erra por 100x.
 * - **`operadora_id` ausente é o consolidado de todas as operadoras**, e não
 *   "operadora desconhecida". São linhas diferentes no banco (há um índice
 *   parcial só para o consolidado), e a métrica sem filtro de operadora usa
 *   exatamente a consolidada.
 *
 * `fonte` é obrigatório (`min_length=1`): é o que permite à tela dizer de onde
 * o número veio, ao lado dele.
 */
export interface BaselineUpsert {
  /** Competência "AAAA-MM". */
  competencia: string;
  /** `null`/ausente = consolidado de **todas** as operadoras. */
  operadora_id?: string | null;
  documentos_enviados: number;
  /** A API recusa (422) quando maior que `documentos_enviados`. */
  documentos_glosados: number;
  valor_glosado_centavos?: number | null;
  horas_conferencia?: number | null;
  fonte: string;
  observacao?: string | null;
}

/** Baseline como a API o devolve. */
export interface BaselineOut {
  id: string;
  competencia: string;
  operadora_id: string | null;
  documentos_enviados: number;
  documentos_glosados: number;
  /** **Inteiro em centavos.** */
  valor_glosado_centavos: number | null;
  horas_conferencia: number | null;
  fonte: string;
  observacao: string | null;
  created_at: string;
  updated_at: string;
}
