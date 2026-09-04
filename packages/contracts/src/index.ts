export type {
  DocumentoCriado,
  DocumentoStatus,
  EuResposta,
  LoginParams,
  LoginResposta,
  MaquinaOut,
  MfaPendenteOut,
  MfaVerificarParams,
  Papel,
  UploadParams,
  UploadResponse,
  UsuarioOut,
} from "./tipos";
export { ehUsuario } from "./tipos";
export { ApiError } from "./erros";
export type { ApiErrorPayload } from "./erros";
export type { OpcoesRequisicao } from "./cliente";
export { login, logout, obterUsuarioAtual, uploadDocumento, verificarMfa } from "./cliente";
