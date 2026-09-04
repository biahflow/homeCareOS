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

export interface MfaVerificarParams {
  /**
   * Os seis dígitos do app autenticador **ou** um código de recuperação
   * (`a1b2c-3d4e5`). Um campo só, como a API exige: dois campos separados
   * diriam a quem sonda qual dos dois caminhos falhou.
   */
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
