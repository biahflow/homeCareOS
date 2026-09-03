"""Registra todos os models SQLAlchemy em `Base.metadata` (lido pelo Alembic autogenerate)."""

from homecareos.db.models.alerta import AlertaEnviado
from homecareos.db.models.baseline import BaselineCompetencia
from homecareos.db.models.codigo_recuperacao_mfa import CodigoRecuperacaoMfa
from homecareos.db.models.documento import Documento
from homecareos.db.models.enums import (
    DocumentoStatus,
    Modalidade,
    PendenciaStatus,
    ResultadoValidacao,
    TipoDocumento,
)
from homecareos.db.models.extracao import Extracao
from homecareos.db.models.log_conferencia import LogConferencia
from homecareos.db.models.operadora import Operadora
from homecareos.db.models.paciente import Paciente
from homecareos.db.models.pendencia import Pendencia
from homecareos.db.models.regra import Regra
from homecareos.db.models.sessao import Sessao
from homecareos.db.models.tentativa_login import TentativaLogin
from homecareos.db.models.token_recuperacao import TokenRecuperacao
from homecareos.db.models.usuario import Usuario
from homecareos.db.models.validacao import Validacao

__all__ = [
    "AlertaEnviado",
    "BaselineCompetencia",
    "CodigoRecuperacaoMfa",
    "Documento",
    "DocumentoStatus",
    "Extracao",
    "LogConferencia",
    "Modalidade",
    "Operadora",
    "Paciente",
    "Pendencia",
    "PendenciaStatus",
    "Regra",
    "ResultadoValidacao",
    "Sessao",
    "TentativaLogin",
    "TipoDocumento",
    "TokenRecuperacao",
    "Usuario",
    "Validacao",
]
