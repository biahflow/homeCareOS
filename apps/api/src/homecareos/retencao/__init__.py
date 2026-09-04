"""Expurgo por retenção de dados (issue #39).

`tentativas_login`, `tokens_recuperacao` e `alertas_enviados` crescem sem
limite e nenhuma delas é só log: as três são consultadas por freios de
segurança ativos, dentro de janelas de tempo (ver `retencao/janelas.py`).
Este pacote orquestra o expurgo — a trava contra retenção menor que a janela
mínima, os lotes e o resumo — sobre as funções de apagar que vivem junto de
cada domínio (`auth/protecao.py`, `auth/recuperacao.py`,
`alerts/repository.py`).

Ver `retencao/cli.py` para o comando (`python -m homecareos.retencao.cli`) e
a seção "Retenção e expurgo de dados" do README de apps/api.
"""
