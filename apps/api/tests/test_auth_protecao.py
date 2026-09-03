"""Testes de `auth/protecao.py` — funções puras, sem banco.

`calcular_atraso` e `ip_do_request` não tocam o Postgres: dá para testar o
teto do atraso (o critério de aceite mais sensível desta issue — sem ele o
próprio atraso vira DoS) sem o teste precisar dormir de verdade.
"""

from __future__ import annotations

from starlette.requests import Request

from homecareos.auth.protecao import calcular_atraso, ip_do_request
from homecareos.config import Settings

BASE = 0.25
MAXIMO = 2.0


def _request(
    *, headers: list[tuple[bytes, bytes]] | None = None, client: tuple[str, int] | None
) -> Request:
    scope = {"type": "http", "headers": headers or [], "client": client}
    return Request(scope)  # type: ignore[arg-type]


# --- calcular_atraso -----------------------------------------------------------


def test_calcular_atraso_com_zero_falhas_e_zero() -> None:
    assert calcular_atraso(0, base=BASE, maximo=MAXIMO) == 0.0


def test_calcular_atraso_progride_com_o_numero_de_falhas() -> None:
    um = calcular_atraso(1, base=BASE, maximo=MAXIMO)
    dois = calcular_atraso(2, base=BASE, maximo=MAXIMO)
    tres = calcular_atraso(3, base=BASE, maximo=MAXIMO)

    assert 0.0 < um < dois < tres


def test_calcular_atraso_respeita_o_teto_com_muitas_falhas() -> None:
    """Critério de aceite central: sem teto, requisições baratas esgotam o
    threadpool síncrono do FastAPI e o próprio atraso vira o ataque."""
    atraso = calcular_atraso(50, base=BASE, maximo=MAXIMO)

    assert atraso == MAXIMO


# --- ip_do_request ---------------------------------------------------------------


def test_ip_do_request_sem_confiar_em_xff_ignora_o_header_e_usa_o_client() -> None:
    settings = Settings(confiar_em_x_forwarded_for=False)
    request = _request(
        headers=[(b"x-forwarded-for", b"203.0.113.9, 10.0.0.1")], client=("198.51.100.7", 12345)
    )

    assert ip_do_request(request, settings) == "198.51.100.7"


def test_ip_do_request_confiando_em_xff_usa_o_primeiro_elemento() -> None:
    settings = Settings(confiar_em_x_forwarded_for=True)
    request = _request(
        headers=[(b"x-forwarded-for", b"203.0.113.9, 10.0.0.1")], client=("198.51.100.7", 12345)
    )

    assert ip_do_request(request, settings) == "203.0.113.9"


def test_ip_do_request_sem_client_devolve_desconhecido_sem_levantar() -> None:
    settings = Settings(confiar_em_x_forwarded_for=False)
    request = _request(client=None)

    assert ip_do_request(request, settings) == "desconhecido"
