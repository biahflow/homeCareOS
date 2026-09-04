"""Testes do expurgo por retenção (issue #39) — contra Postgres real
(localhost:5434).

O banco é compartilhado com o desenvolvimento e a suíte roda no mesmo
processo dos outros testes de integração. `retencao.service.expurgar` é, por
natureza, um `DELETE` GLOBAL não escopado — apagar "toda linha antiga" é
justamente o que um expurgo é, e não dá para filtrar por sentinela como os
demais testes fazem no `WHERE`.

A estratégia aqui é a mesma ironia que `test_api_login_bloqueio` já assume em
`test_limpar_tentativas_antigas_remove_a_antiga_e_preserva_a_recente`
(`removidas >= 1`, não um número exato): **nunca afirmar a contagem GLOBAL
exata** devolvida por `expurgar`, só um piso (`>=`) quando ela importa, e
confirmar o resultado sobre os próprios ids capturados antes da chamada — via
`session.get(...)` — que é preciso mesmo com outra linha antiga (de outro
teste, de outra tarefa) coexistindo no mesmo `DELETE`. Nenhum teste depende de
a tabela estar vazia antes de rodar, e o teardown de cada teste apaga só as
linhas que ELE criou, pelo sentinela único (e-mail/ip/usuário/destinatário) —
nunca `TRUNCATE`, nunca `DELETE` geral.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from homecareos.auth import senhas
from homecareos.auth.schema import AcaoAuditoriaUsuario, Papel
from homecareos.config import Settings, get_settings
from homecareos.db.models import (
    AlertaEnviado,
    AuditoriaUsuario,
    ConsumoRateLimit,
    TentativaLogin,
    TokenRecuperacao,
    Usuario,
)
from homecareos.db.session import get_sessionmaker
from homecareos.limites.protecao import JANELA as JANELA_RATE_LIMIT_ROTAS
from homecareos.retencao import cli as retencao_cli
from homecareos.retencao.errors import RetencaoConfigError, RetencaoInvalidaError
from homecareos.retencao.janelas import (
    FATOR_MARGEM_SEGURANCA,
    MINIMO_AUDITORIA_USUARIOS,
    JanelaSeguranca,
    pisos_auditoria_usuarios,
    pisos_consumos_rate_limit,
)
from homecareos.retencao.schema import ResumoExpurgo
from homecareos.retencao.service import expurgar

pytestmark = pytest.mark.integration

SENHA_DE_TESTE = "senha-de-teste-retencao"


@pytest.fixture
def sessao() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def settings() -> Settings:
    return get_settings()


# --- sentinelas ------------------------------------------------------------


@pytest.fixture
def contexto_login(sessao: Session) -> Iterator[tuple[str, str]]:
    """(email, ip) únicos para `tentativas_login`. Teardown apaga só as linhas
    que batem com esse e-mail OU esse ip."""
    email = f"retencao-{uuid.uuid4()}@teste.local"
    ip = f"ip-retencao-{uuid.uuid4()}"
    yield email, ip
    sessao.execute(
        text("delete from tentativas_login where email_tentado = :email or ip = :ip"),
        {"email": email, "ip": ip},
    )
    sessao.commit()


def _criar_usuario(session: Session) -> Usuario:
    usuario = Usuario(
        nome="Pessoa de Teste - Retenção",
        email=f"retencao-{uuid.uuid4()}@teste.local",
        senha_hash=senhas.gerar_hash(SENHA_DE_TESTE),
        papel=Papel.CONFERENTE.value,
        ativo=True,
    )
    session.add(usuario)
    session.commit()
    return usuario


def _limpar_usuario(session: Session, usuario_id: uuid.UUID) -> None:
    session.execute(
        text("delete from tokens_recuperacao where usuario_id = :id"), {"id": usuario_id}
    )
    session.execute(text("delete from usuarios where id = :id"), {"id": usuario_id})
    session.commit()


@pytest.fixture
def usuario(sessao: Session) -> Iterator[Usuario]:
    linha = _criar_usuario(sessao)
    yield linha
    _limpar_usuario(sessao, linha.id)


@pytest.fixture
def destinatario_alerta() -> Iterator[str]:
    """Telefone único para `alertas_enviados`. Teardown apaga só as linhas
    desse destinatário."""
    destinatario = f"5521{uuid.uuid4().int % 10**9:09d}"
    yield destinatario
    with get_sessionmaker()() as sessao_teardown:
        sessao_teardown.execute(
            text("delete from alertas_enviados where destinatario = :destinatario"),
            {"destinatario": destinatario},
        )
        sessao_teardown.commit()


def _alerta(*, destinatario: str, created_at: datetime, status: str = "enviado") -> AlertaEnviado:
    return AlertaEnviado(
        tipo="documento_incompleto_critico",
        chave=f"retencao-teste:{uuid.uuid4()}",
        destinatario=destinatario,
        mensagem="mensagem de teste do expurgo por retenção",
        status=status,
        created_at=created_at,
    )


# --- tentativas_login --------------------------------------------------------


def test_tentativas_login_apaga_a_antiga_e_preserva_a_recente(
    sessao: Session, settings: Settings, contexto_login: tuple[str, str]
) -> None:
    email, ip = contexto_login
    agora = datetime.now(UTC)
    antiga = TentativaLogin(
        email_tentado=email, ip=ip, sucesso=False, created_at=agora - timedelta(days=200)
    )
    recente = TentativaLogin(email_tentado=email, ip=ip, sucesso=False, created_at=agora)
    sessao.add_all([antiga, recente])
    sessao.commit()
    antiga_id, recente_id = antiga.id, recente.id

    resumo = expurgar(
        sessao, settings, tabelas=["tentativas_login"], lote=1000, dry_run=False, agora=agora
    )

    assert resumo.tabelas["tentativas_login"].apagadas >= 1
    assert sessao.get(TentativaLogin, antiga_id) is None
    assert sessao.get(TentativaLogin, recente_id) is not None


def test_tentativas_login_lote_menor_que_o_volume_apaga_tudo_ao_fim(
    sessao: Session, settings: Settings, contexto_login: tuple[str, str]
) -> None:
    """Cinco linhas antigas, lote de duas: ao fim, as cinco saem — prova que o
    laço de lotes continua até não sobrar nenhuma, mesmo com `lote < volume`.
    """
    email, ip = contexto_login
    agora = datetime.now(UTC)
    linhas = [
        TentativaLogin(
            email_tentado=email,
            ip=ip,
            sucesso=False,
            created_at=agora - timedelta(days=200, hours=i),
        )
        for i in range(5)
    ]
    sessao.add_all(linhas)
    sessao.commit()
    ids = [linha.id for linha in linhas]

    resumo = expurgar(
        sessao, settings, tabelas=["tentativas_login"], lote=2, dry_run=False, agora=agora
    )

    assert resumo.tabelas["tentativas_login"].apagadas >= 5
    for id_ in ids:
        assert sessao.get(TentativaLogin, id_) is None


def test_tentativas_login_dry_run_conta_e_nao_apaga(
    sessao: Session, settings: Settings, contexto_login: tuple[str, str]
) -> None:
    email, ip = contexto_login
    agora = datetime.now(UTC)
    antiga = TentativaLogin(
        email_tentado=email, ip=ip, sucesso=False, created_at=agora - timedelta(days=200)
    )
    sessao.add(antiga)
    sessao.commit()
    antiga_id = antiga.id

    resumo = expurgar(
        sessao, settings, tabelas=["tentativas_login"], lote=1000, dry_run=True, agora=agora
    )

    assert resumo.dry_run is True
    assert resumo.tabelas["tentativas_login"].apagadas >= 1
    # Nada foi apagado: a linha antiga ainda está lá.
    assert sessao.get(TentativaLogin, antiga_id) is not None


def test_tentativas_login_retencao_menor_que_a_janela_falha_e_nao_apaga(
    sessao: Session, settings: Settings, contexto_login: tuple[str, str]
) -> None:
    email, ip = contexto_login
    agora = datetime.now(UTC)
    antiga = TentativaLogin(
        email_tentado=email, ip=ip, sucesso=False, created_at=agora - timedelta(days=400)
    )
    sessao.add(antiga)
    sessao.commit()
    antiga_id = antiga.id

    invalida = settings.model_copy(update={"retencao_tentativas_login_dias": 0})

    # O piso do teste roda mesmo em dry-run: a trava não pode depender de o
    # operador escolher rodar de verdade para ser flagrada.
    with pytest.raises(RetencaoInvalidaError):
        expurgar(
            sessao, invalida, tabelas=["tentativas_login"], lote=1000, dry_run=True, agora=agora
        )
    with pytest.raises(RetencaoInvalidaError):
        expurgar(
            sessao, invalida, tabelas=["tentativas_login"], lote=1000, dry_run=False, agora=agora
        )

    assert sessao.get(TentativaLogin, antiga_id) is not None


# --- tokens_recuperacao ------------------------------------------------------


def test_tokens_recuperacao_apaga_o_antigo_e_preserva_o_recente(
    sessao: Session, settings: Settings, usuario: Usuario
) -> None:
    agora = datetime.now(UTC)
    antigo = TokenRecuperacao(
        usuario_id=usuario.id,
        token_hash=f"hash-antigo-{uuid.uuid4()}",
        expires_at=agora - timedelta(days=39),
        used_at=agora - timedelta(days=39),
        created_at=agora - timedelta(days=40),
    )
    recente = TokenRecuperacao(
        usuario_id=usuario.id,
        token_hash=f"hash-recente-{uuid.uuid4()}",
        expires_at=agora + timedelta(minutes=30),
        created_at=agora,
    )
    sessao.add_all([antigo, recente])
    sessao.commit()
    antigo_id, recente_id = antigo.id, recente.id

    resumo = expurgar(
        sessao, settings, tabelas=["tokens_recuperacao"], lote=1000, dry_run=False, agora=agora
    )

    assert resumo.tabelas["tokens_recuperacao"].apagadas >= 1
    assert sessao.get(TokenRecuperacao, antigo_id) is None
    assert sessao.get(TokenRecuperacao, recente_id) is not None


def test_tokens_recuperacao_nunca_apaga_token_ainda_valido_e_nao_usado_mesmo_velho(
    sessao: Session, settings: Settings, usuario: Usuario
) -> None:
    """Critério de aceite 2: `created_at` velho, mas `used_at IS NULL AND
    expires_at > agora` — nunca sai, mesmo passando da retenção configurada.
    """
    agora = datetime.now(UTC)
    valido_e_velho = TokenRecuperacao(
        usuario_id=usuario.id,
        token_hash=f"hash-valido-velho-{uuid.uuid4()}",
        # criado há 40 dias (além da retenção de 30), mas ainda não expirou.
        expires_at=agora + timedelta(minutes=30),
        used_at=None,
        created_at=agora - timedelta(days=40),
    )
    sessao.add(valido_e_velho)
    sessao.commit()
    id_ = valido_e_velho.id

    resumo = expurgar(
        sessao, settings, tabelas=["tokens_recuperacao"], lote=1000, dry_run=False, agora=agora
    )

    assert sessao.get(TokenRecuperacao, id_) is not None
    # E o dry-run concorda: não conta essa linha como candidata.
    resumo_dry = expurgar(
        sessao, settings, tabelas=["tokens_recuperacao"], lote=1000, dry_run=True, agora=agora
    )
    assert (
        resumo.tabelas["tokens_recuperacao"].apagadas
        == resumo_dry.tabelas["tokens_recuperacao"].apagadas
    )


def test_tokens_recuperacao_dry_run_conta_e_nao_apaga(
    sessao: Session, settings: Settings, usuario: Usuario
) -> None:
    agora = datetime.now(UTC)
    antigo = TokenRecuperacao(
        usuario_id=usuario.id,
        token_hash=f"hash-dryrun-{uuid.uuid4()}",
        expires_at=agora - timedelta(days=39),
        used_at=agora - timedelta(days=39),
        created_at=agora - timedelta(days=40),
    )
    sessao.add(antigo)
    sessao.commit()
    antigo_id = antigo.id

    resumo = expurgar(
        sessao, settings, tabelas=["tokens_recuperacao"], lote=1000, dry_run=True, agora=agora
    )

    assert resumo.dry_run is True
    assert resumo.tabelas["tokens_recuperacao"].apagadas >= 1
    assert sessao.get(TokenRecuperacao, antigo_id) is not None


def test_tokens_recuperacao_retencao_menor_que_a_janela_falha_e_nao_apaga(
    sessao: Session, settings: Settings, usuario: Usuario
) -> None:
    agora = datetime.now(UTC)
    antigo = TokenRecuperacao(
        usuario_id=usuario.id,
        token_hash=f"hash-invalida-{uuid.uuid4()}",
        expires_at=agora - timedelta(days=59),
        used_at=agora - timedelta(days=59),
        created_at=agora - timedelta(days=60),
    )
    sessao.add(antigo)
    sessao.commit()
    antigo_id = antigo.id

    invalida = settings.model_copy(update={"retencao_tokens_recuperacao_dias": 0})

    with pytest.raises(RetencaoInvalidaError):
        expurgar(
            sessao, invalida, tabelas=["tokens_recuperacao"], lote=1000, dry_run=False, agora=agora
        )

    assert sessao.get(TokenRecuperacao, antigo_id) is not None


# --- alertas_enviados ---------------------------------------------------------


def test_alertas_enviados_apaga_o_antigo_e_preserva_o_recente(
    sessao: Session, settings: Settings, destinatario_alerta: str
) -> None:
    agora = datetime.now(UTC)
    antigo = _alerta(destinatario=destinatario_alerta, created_at=agora - timedelta(days=100))
    recente = _alerta(destinatario=destinatario_alerta, created_at=agora)
    sessao.add_all([antigo, recente])
    sessao.commit()
    antigo_id, recente_id = antigo.id, recente.id

    resumo = expurgar(
        sessao, settings, tabelas=["alertas_enviados"], lote=1000, dry_run=False, agora=agora
    )

    assert resumo.tabelas["alertas_enviados"].apagadas >= 1
    assert sessao.get(AlertaEnviado, antigo_id) is None
    assert sessao.get(AlertaEnviado, recente_id) is not None


def test_alertas_enviados_dry_run_conta_e_nao_apaga(
    sessao: Session, settings: Settings, destinatario_alerta: str
) -> None:
    agora = datetime.now(UTC)
    antigo = _alerta(destinatario=destinatario_alerta, created_at=agora - timedelta(days=100))
    sessao.add(antigo)
    sessao.commit()
    antigo_id = antigo.id

    resumo = expurgar(
        sessao, settings, tabelas=["alertas_enviados"], lote=1000, dry_run=True, agora=agora
    )

    assert resumo.dry_run is True
    assert resumo.tabelas["alertas_enviados"].apagadas >= 1
    assert sessao.get(AlertaEnviado, antigo_id) is not None


def test_alertas_enviados_retencao_menor_que_a_janela_falha_e_nao_apaga(
    sessao: Session, settings: Settings, destinatario_alerta: str
) -> None:
    agora = datetime.now(UTC)
    antigo = _alerta(destinatario=destinatario_alerta, created_at=agora - timedelta(days=200))
    sessao.add(antigo)
    sessao.commit()
    antigo_id = antigo.id

    # Piso = 2x o cooldown (24h por padrão) = 48h = 2 dias; 1 dia viola.
    invalida = settings.model_copy(update={"retencao_alertas_enviados_dias": 1})

    with pytest.raises(RetencaoInvalidaError) as excinfo:
        expurgar(
            sessao, invalida, tabelas=["alertas_enviados"], lote=1000, dry_run=False, agora=agora
        )
    assert "cooldown" in str(excinfo.value)

    assert sessao.get(AlertaEnviado, antigo_id) is not None


# --- orquestração: tabela desconhecida, lote inválido ------------------------


def test_expurgar_tabela_desconhecida_falha(sessao: Session, settings: Settings) -> None:
    agora = datetime.now(UTC)
    with pytest.raises(RetencaoConfigError):
        expurgar(
            sessao,
            settings,
            tabelas=["tabela_que_nao_existe"],
            lote=1000,
            dry_run=True,
            agora=agora,
        )


def test_expurgar_lote_nao_positivo_falha(sessao: Session, settings: Settings) -> None:
    agora = datetime.now(UTC)
    with pytest.raises(RetencaoConfigError):
        expurgar(sessao, settings, tabelas=None, lote=0, dry_run=True, agora=agora)


# --- CLI: wiring de argumentos, sem tocar o banco -----------------------------


def test_cli_sem_flags_e_dry_run_e_usa_o_lote_da_configuracao(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    chamadas: list[dict[str, object]] = []

    def _fake_expurgar(session: object, settings: Settings, **kwargs: object) -> ResumoExpurgo:
        chamadas.append(kwargs)
        return ResumoExpurgo(
            dry_run=bool(kwargs["dry_run"]), executado_em=datetime.now(UTC), tabelas={}
        )

    monkeypatch.setattr(retencao_cli, "expurgar", _fake_expurgar)

    codigo = retencao_cli.main([])

    assert codigo == 0
    assert chamadas[0]["dry_run"] is True
    assert chamadas[0]["tabelas"] is None
    assert chamadas[0]["lote"] == get_settings().retencao_tamanho_lote
    saida = json.loads(capsys.readouterr().out)
    assert saida["dry_run"] is True


def test_cli_executar_filtra_tabela_e_sobrescreve_o_lote(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    chamadas: list[dict[str, object]] = []

    def _fake_expurgar(session: object, settings: Settings, **kwargs: object) -> ResumoExpurgo:
        chamadas.append(kwargs)
        return ResumoExpurgo(
            dry_run=bool(kwargs["dry_run"]), executado_em=datetime.now(UTC), tabelas={}
        )

    monkeypatch.setattr(retencao_cli, "expurgar", _fake_expurgar)

    codigo = retencao_cli.main(["--executar", "--tabela", "alertas_enviados", "--lote", "7"])

    assert codigo == 0
    assert chamadas[0]["dry_run"] is False
    assert chamadas[0]["tabelas"] == ["alertas_enviados"]
    assert chamadas[0]["lote"] == 7


def test_cli_retencao_invalida_sai_com_codigo_1_e_mensagem_no_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _fake_expurgar(*args: object, **kwargs: object) -> ResumoExpurgo:
        raise RetencaoInvalidaError("retenção configurada abaixo do mínimo aceitável")

    monkeypatch.setattr(retencao_cli, "expurgar", _fake_expurgar)

    codigo = retencao_cli.main([])

    assert codigo == 1
    assert "retenção configurada abaixo do mínimo aceitável" in capsys.readouterr().err


def test_cli_dry_run_e_executar_juntos_e_invalido(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        retencao_cli.main(["--dry-run", "--executar"])
    assert excinfo.value.code == 2


# --- auditoria_usuarios ------------------------------------------------------
#
# A quarta tabela do registro, e a única cujo piso não é janela de freio de
# segurança — ver `retencao/janelas.py`.


@pytest.fixture
def usuario_auditado(sessao: Session) -> Iterator[Usuario]:
    """Usuário sentinela que é ator E alvo dos eventos de auditoria do teste.

    `auditoria_usuarios` tem FK para `usuarios` nos dois papéis, então não há
    evento sem pessoa. O teardown apaga primeiro os eventos que citam este
    usuário (senão a FK segura a exclusão dele) e depois o próprio usuário —
    só o que o teste criou, nunca `TRUNCATE`.
    """
    linha = _criar_usuario(sessao)
    yield linha
    sessao.execute(
        text("delete from auditoria_usuarios where usuario_id = :id or alvo_usuario_id = :id"),
        {"id": linha.id},
    )
    sessao.commit()
    _limpar_usuario(sessao, linha.id)


def _evento(*, usuario: Usuario, created_at: datetime) -> AuditoriaUsuario:
    return AuditoriaUsuario(
        usuario=usuario.email,
        usuario_id=usuario.id,
        alvo_usuario_id=usuario.id,
        alvo_email=usuario.email,
        acao=AcaoAuditoriaUsuario.ALTERACAO.value,
        mudancas={"nome": {"de": "Nome Antigo", "para": "Nome Novo"}},
        created_at=created_at,
    )


def test_auditoria_usuarios_apaga_o_antigo_e_preserva_o_recente(
    sessao: Session, settings: Settings, usuario_auditado: Usuario
) -> None:
    agora = datetime.now(UTC)
    # 2000 dias > a retenção default de 1825 (5 anos).
    antigo = _evento(usuario=usuario_auditado, created_at=agora - timedelta(days=2000))
    recente = _evento(usuario=usuario_auditado, created_at=agora)
    sessao.add_all([antigo, recente])
    sessao.commit()
    antigo_id, recente_id = antigo.id, recente.id

    resumo = expurgar(
        sessao, settings, tabelas=["auditoria_usuarios"], lote=1000, dry_run=False, agora=agora
    )

    assert resumo.tabelas["auditoria_usuarios"].apagadas >= 1
    assert sessao.get(AuditoriaUsuario, antigo_id) is None
    assert sessao.get(AuditoriaUsuario, recente_id) is not None


def test_auditoria_usuarios_lote_menor_que_o_volume_apaga_tudo_ao_fim(
    sessao: Session, settings: Settings, usuario_auditado: Usuario
) -> None:
    """Cinco eventos antigos, lote de dois: ao fim, os cinco saem."""
    agora = datetime.now(UTC)
    linhas = [
        _evento(usuario=usuario_auditado, created_at=agora - timedelta(days=2000, hours=i))
        for i in range(5)
    ]
    sessao.add_all(linhas)
    sessao.commit()
    ids = [linha.id for linha in linhas]

    resumo = expurgar(
        sessao, settings, tabelas=["auditoria_usuarios"], lote=2, dry_run=False, agora=agora
    )

    assert resumo.tabelas["auditoria_usuarios"].apagadas >= 5
    for id_ in ids:
        assert sessao.get(AuditoriaUsuario, id_) is None


def test_auditoria_usuarios_dry_run_conta_e_nao_apaga(
    sessao: Session, settings: Settings, usuario_auditado: Usuario
) -> None:
    agora = datetime.now(UTC)
    antigo = _evento(usuario=usuario_auditado, created_at=agora - timedelta(days=2000))
    sessao.add(antigo)
    sessao.commit()
    antigo_id = antigo.id

    resumo = expurgar(
        sessao, settings, tabelas=["auditoria_usuarios"], lote=1000, dry_run=True, agora=agora
    )

    assert resumo.dry_run is True
    assert resumo.tabelas["auditoria_usuarios"].apagadas >= 1
    assert sessao.get(AuditoriaUsuario, antigo_id) is not None


def test_auditoria_usuarios_retencao_abaixo_do_piso_falha_e_nao_apaga(
    sessao: Session, settings: Settings, usuario_auditado: Usuario
) -> None:
    """Trinta dias violam o piso de um ano — e a recusa fala do PROPÓSITO da
    tabela, não de um freio de segurança, que aqui não existe.
    """
    agora = datetime.now(UTC)
    antigo = _evento(usuario=usuario_auditado, created_at=agora - timedelta(days=2000))
    sessao.add(antigo)
    sessao.commit()
    antigo_id = antigo.id

    invalida = settings.model_copy(update={"retencao_auditoria_usuarios_dias": 30})

    # Como nas outras tabelas, a trava roda mesmo em dry-run.
    with pytest.raises(RetencaoInvalidaError):
        expurgar(
            sessao, invalida, tabelas=["auditoria_usuarios"], lote=1000, dry_run=True, agora=agora
        )
    with pytest.raises(RetencaoInvalidaError) as excinfo:
        expurgar(
            sessao, invalida, tabelas=["auditoria_usuarios"], lote=1000, dry_run=False, agora=agora
        )

    mensagem = str(excinfo.value)
    assert "auditoria administrativa deixa de responder" in mensagem
    # A razão errada seria pior que razão nenhuma: não há freio lendo esta tabela.
    assert "desarmar" not in mensagem
    assert "margem de segurança aplicada" not in mensagem

    assert sessao.get(AuditoriaUsuario, antigo_id) is not None


def test_expurgo_sem_filtro_cobre_todas_as_tabelas_do_registro(
    sessao: Session, settings: Settings
) -> None:
    """Sem `--tabela`, toda tabela registrada entra junto — é o que faz o serviço
    `api-retencao` do Compose cobrir uma tabela nova sem serviço novo.

    A lista é escrita à mão de propósito, em vez de comparada com `NOMES_TABELAS`
    (o que seria tautológico): quem acrescentar uma tabela ao registro vê este
    teste falhar e precisa confirmar que ela **deve mesmo** ser expurgada pelo
    cron. É a pergunta que ninguém faz sozinho.
    """
    agora = datetime.now(UTC)

    resumo = expurgar(sessao, settings, tabelas=None, lote=1000, dry_run=True, agora=agora)

    assert set(resumo.tabelas) == {
        "tentativas_login",
        "tokens_recuperacao",
        "alertas_enviados",
        "auditoria_usuarios",
        "consumos_rate_limit",
    }


# --- os dois tipos de piso ----------------------------------------------------


def test_piso_de_freio_de_seguranca_dobra_a_janela() -> None:
    janela = JanelaSeguranca(descricao="janela qualquer", janela=timedelta(hours=1), origem="teste")

    assert janela.piso_minimo == timedelta(hours=1) * FATOR_MARGEM_SEGURANCA


def test_piso_de_valor_de_auditoria_e_o_minimo_declarado_sem_margem() -> None:
    """O mínimo é o mínimo: sem freio, não há relógio a perder, e dobrá-lo em
    silêncio inventaria política de retenção.
    """
    (piso,) = pisos_auditoria_usuarios(get_settings())

    assert piso.piso_minimo == MINIMO_AUDITORIA_USUARIOS == timedelta(days=365)
    # E a recusa não empresta o vocabulário do piso de freio.
    assert "desarmar" not in piso.motivo()
    assert f"{FATOR_MARGEM_SEGURANCA}x" not in piso.motivo()


# --- consumos_rate_limit ------------------------------------------------------


@pytest.fixture
def chave_de_consumo(sessao: Session) -> Iterator[str]:
    """Chave sentinela própria deste teste — o banco é compartilhado."""
    chave = f"usuario:{uuid.uuid4()}"
    yield chave
    sessao.execute(text("delete from consumos_rate_limit where chave = :chave"), {"chave": chave})
    sessao.commit()


def _consumo(*, chave: str, created_at: datetime) -> ConsumoRateLimit:
    return ConsumoRateLimit(chave=chave, recurso="upload_documento", created_at=created_at)


def test_consumos_rate_limit_apaga_o_antigo_e_preserva_o_recente(
    sessao: Session, settings: Settings, chave_de_consumo: str
) -> None:
    agora = datetime.now(UTC)
    antigo = _consumo(chave=chave_de_consumo, created_at=agora - timedelta(days=60))
    recente = _consumo(chave=chave_de_consumo, created_at=agora)
    sessao.add_all([antigo, recente])
    sessao.commit()
    antigo_id, recente_id = antigo.id, recente.id

    resumo = expurgar(
        sessao, settings, tabelas=["consumos_rate_limit"], lote=1000, dry_run=False, agora=agora
    )

    assert resumo.tabelas["consumos_rate_limit"].apagadas >= 1
    assert sessao.get(ConsumoRateLimit, antigo_id) is None
    assert sessao.get(ConsumoRateLimit, recente_id) is not None


def test_consumos_rate_limit_dry_run_conta_e_nao_apaga(
    sessao: Session, settings: Settings, chave_de_consumo: str
) -> None:
    agora = datetime.now(UTC)
    antigo = _consumo(chave=chave_de_consumo, created_at=agora - timedelta(days=60))
    sessao.add(antigo)
    sessao.commit()
    antigo_id = antigo.id

    resumo = expurgar(
        sessao, settings, tabelas=["consumos_rate_limit"], lote=1000, dry_run=True, agora=agora
    )

    assert resumo.dry_run is True
    assert resumo.tabelas["consumos_rate_limit"].apagadas >= 1
    assert sessao.get(ConsumoRateLimit, antigo_id) is not None


def test_consumos_rate_limit_retencao_dentro_da_janela_do_freio_e_recusada(
    sessao: Session, settings: Settings, chave_de_consumo: str
) -> None:
    """Expurgar dentro da janela do freio devolve cota a quem estourou o limite.

    É o mesmo perigo de `tentativas_login`: a tabela não é só log, é o contador
    que uma decisão de segurança consulta. A retenção mínima aqui é o dobro da
    janela de contagem do ADR 0005 — e como a janela é de uma hora, qualquer
    retenção em dias passa. Este teste existe para o dia em que alguém achar que
    "uma hora de retenção basta, a janela é de uma hora".
    """
    agora = datetime.now(UTC)
    antigo = _consumo(chave=chave_de_consumo, created_at=agora - timedelta(days=60))
    sessao.add(antigo)
    sessao.commit()
    antigo_id = antigo.id

    # 0 dias viola o piso (2x a janela de 1h do freio).
    invalida = settings.model_copy(update={"retencao_consumos_rate_limit_dias": 0})

    with pytest.raises(RetencaoInvalidaError) as excinfo:
        expurgar(
            sessao,
            invalida,
            tabelas=["consumos_rate_limit"],
            lote=1000,
            dry_run=False,
            agora=agora,
        )
    mensagem = str(excinfo.value)
    assert "rate limit" in mensagem
    assert "desarmar" in mensagem  # é piso de freio, não de propósito

    assert sessao.get(ConsumoRateLimit, antigo_id) is not None


def test_o_piso_do_contador_acompanha_a_janela_do_freio() -> None:
    """O piso é derivado da janela real, não de um número copiado.

    Se alguém mudar `limites/protecao.JANELA`, o piso muda junto — é o mesmo
    contrato das outras janelas hardcoded do módulo.
    """
    (piso,) = pisos_consumos_rate_limit(get_settings())

    assert piso.piso_minimo == JANELA_RATE_LIMIT_ROTAS * FATOR_MARGEM_SEGURANCA
