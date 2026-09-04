"""Testes do docker-entrypoint.sh: um caso por linha da tabela de decisão
descrita no comentário do próprio script. Executa o script de verdade via
`sh` e `subprocess`, sem Docker — precisa rodar igual no CI e no Mac.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "docker-entrypoint.sh"


def _rodar(env_extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Roda o script com `echo ok` como comando final. Se o script recusar
    arrancar, `exec "$@"` nunca acontece e "ok" não aparece no stdout — é
    assim que o teste distingue "passou a checagem" de "recusou arrancar"."""
    env = os.environ.copy()
    env.pop("HOMECAREOS_SKIP_LOCK_CHECK", None)
    env.update(env_extra)
    return subprocess.run(
        ["sh", str(SCRIPT), "echo", "ok"],
        env=env,
        capture_output=True,
        text=True,
    )


def test_locks_identicos_segue_e_executa_o_comando(tmp_path: Path) -> None:
    imagem = tmp_path / "image.lock"
    montado = tmp_path / "uv.lock"
    conteudo = "mesmo conteudo\n"
    imagem.write_text(conteudo)
    montado.write_text(conteudo)

    resultado = _rodar(
        {
            "HOMECAREOS_IMAGE_LOCK": str(imagem),
            "HOMECAREOS_MOUNTED_LOCK": str(montado),
        }
    )

    assert resultado.returncode == 0
    assert "ok" in resultado.stdout


def test_locks_diferentes_recusa_arrancar(tmp_path: Path) -> None:
    imagem = tmp_path / "image.lock"
    montado = tmp_path / "uv.lock"
    imagem.write_text("versao antiga\n")
    montado.write_text("versao nova\n")

    resultado = _rodar(
        {
            "HOMECAREOS_IMAGE_LOCK": str(imagem),
            "HOMECAREOS_MOUNTED_LOCK": str(montado),
        }
    )

    assert resultado.returncode == 1
    assert "ok" not in resultado.stdout
    # O comando sugerido tem de ser o que realmente reconstrói os quatro
    # serviços: `docker compose build` sem `--profile tools` alcança só o
    # `api`, e quem viu o erro no `api-migrate` rodaria de novo no vazio.
    assert "docker compose --profile tools build" in resultado.stderr
    # A mensagem tem de chegar literal. O heredoc do script usa delimitador
    # entre aspas justamente para isto: sem ele o shell faz expansão de crase,
    # tenta executar o que está entre elas ("docker: not found" no stderr) e
    # entrega ao usuário um texto com buracos.
    assert "`--profile tools`" in resultado.stderr
    assert "not found" not in resultado.stderr


def test_lock_montado_ausente_segue_silencioso(tmp_path: Path) -> None:
    """Produção roda a imagem sem volume nenhum: sem uv.lock montado, a
    verificação não tem o que comparar e não pode travar o arranque."""
    imagem = tmp_path / "image.lock"
    imagem.write_text("qualquer coisa\n")

    resultado = _rodar(
        {
            "HOMECAREOS_IMAGE_LOCK": str(imagem),
            "HOMECAREOS_MOUNTED_LOCK": str(tmp_path / "nao-existe.lock"),
        }
    )

    assert resultado.returncode == 0
    assert "ok" in resultado.stdout


def test_lock_da_imagem_ausente_segue_silencioso(tmp_path: Path) -> None:
    """Imagem construída antes deste script existir não tem .image-uv.lock;
    a verificação não pode quebrar quem já está rodando essa imagem antiga."""
    montado = tmp_path / "uv.lock"
    montado.write_text("qualquer coisa\n")

    resultado = _rodar(
        {
            "HOMECAREOS_IMAGE_LOCK": str(tmp_path / "nao-existe.lock"),
            "HOMECAREOS_MOUNTED_LOCK": str(montado),
        }
    )

    assert resultado.returncode == 0
    assert "ok" in resultado.stdout


def test_skip_lock_check_ignora_divergencia_com_aviso(tmp_path: Path) -> None:
    imagem = tmp_path / "image.lock"
    montado = tmp_path / "uv.lock"
    imagem.write_text("versao antiga\n")
    montado.write_text("versao nova\n")

    resultado = _rodar(
        {
            "HOMECAREOS_IMAGE_LOCK": str(imagem),
            "HOMECAREOS_MOUNTED_LOCK": str(montado),
            "HOMECAREOS_SKIP_LOCK_CHECK": "1",
        }
    )

    assert resultado.returncode == 0
    assert "ok" in resultado.stdout
    assert "AVISO" in resultado.stderr
