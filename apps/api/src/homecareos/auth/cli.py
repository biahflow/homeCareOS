"""Criação de usuário por linha de comando — como nasce o primeiro acesso.

    python -m homecareos.auth.cli criar --nome "Ana" --email ana@x.com \\
        --papel conferente

É CLI e não endpoint porque a matriz de papéis aprovada não diz quem
administra usuário. Decidir isso sem o cliente seria inventar requisito, e um
`POST /api/usuarios` aberto ao papel errado é pior que não ter endpoint nenhum:
quem pudesse criar usuário poderia criar um `gestor` e escalar sozinho.

**A senha nunca vem em argumento de linha de comando.** Ela é lida por
`getpass`, sem eco. Um `--senha` ficaria no histórico do shell, apareceria em
`ps` para qualquer outro usuário da máquina e entraria em log de auditoria de
sistema — três vazamentos que nenhum hash no banco desfaz.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy.exc import IntegrityError

from homecareos.auth import senhas
from homecareos.auth.router import normalizar_email
from homecareos.auth.schema import Papel
from homecareos.db.models import Usuario
from homecareos.db.session import get_sessionmaker


def _ler_senha() -> str:
    """Lê a senha duas vezes, sem eco, e recusa divergência e senha vazia.

    A confirmação existe porque não há recuperação de senha nesta entrega: um
    erro de digitação aqui criaria um usuário que ninguém consegue usar e que
    só um segundo comando conserta.
    """
    senha = getpass.getpass("Senha: ")
    if not senha:
        raise ValueError("senha vazia")
    if senha != getpass.getpass("Confirme a senha: "):
        raise ValueError("as senhas não conferem")
    return senha


def criar(nome: str, email: str, papel: str) -> int:
    """Cria o usuário e devolve o código de saída do processo."""
    try:
        papel_valido = Papel(papel)
    except ValueError:
        validos = ", ".join(p.value for p in Papel)
        print(f"papel inválido: {papel!r}. Papéis válidos: {validos}", file=sys.stderr)
        return 1

    try:
        senha = _ler_senha()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    usuario = Usuario(
        nome=nome,
        email=normalizar_email(email),
        senha_hash=senhas.gerar_hash(senha),
        papel=papel_valido.value,
    )
    # A senha sai de escopo aqui: dela só sobrevive o hash acima. Nada de
    # `print` do objeto, nada de log — o `repr` de `Usuario` já não a contém
    # porque ela nunca virou atributo.
    with get_sessionmaker()() as session:
        session.add(usuario)
        try:
            session.commit()
        except IntegrityError:
            # É o índice único de `usuarios.email` que decide a colisão, e não
            # um SELECT prévio: entre a consulta e o INSERT cabe outro cadastro.
            session.rollback()
            print(f"já existe usuário com o e-mail {normalizar_email(email)!r}", file=sys.stderr)
            return 1
        print(f"usuário criado: {usuario.email} ({papel_valido.value})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m homecareos.auth.cli",
        description="Administração de usuários da conferência (issue #30).",
    )
    subcomandos = parser.add_subparsers(dest="comando", required=True)

    criar_parser = subcomandos.add_parser("criar", help="Cria um usuário (senha lida por prompt)")
    criar_parser.add_argument("--nome", required=True)
    criar_parser.add_argument("--email", required=True)
    criar_parser.add_argument(
        "--papel", required=True, help=", ".join(papel.value for papel in Papel)
    )

    args = parser.parse_args(argv)
    if args.comando == "criar":
        return criar(nome=args.nome, email=args.email, papel=args.papel)
    return 1  # inalcançável: `required=True` já barra comando desconhecido


if __name__ == "__main__":
    raise SystemExit(main())
