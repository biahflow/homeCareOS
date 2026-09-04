"""Expurgo por retenção de dados (issue #39).

Quatro tabelas crescem sem limite e nenhuma delas é só log.
`tentativas_login`, `tokens_recuperacao` e `alertas_enviados` são consultadas
por freios de segurança ativos, dentro de janelas de tempo; `auditoria_usuarios`
não é lida por freio nenhum, mas responde a "quem autorizou o quê" em
investigação que acontece anos depois. As duas naturezas de piso mínimo estão
em `retencao/janelas.py`. Este pacote orquestra o expurgo — a trava contra
retenção menor que o piso, os lotes e o resumo — sobre as funções de apagar
que vivem junto de cada domínio (`auth/protecao.py`, `auth/recuperacao.py`,
`alerts/repository.py`, `auth/auditoria.py`).

Ver `retencao/cli.py` para o comando (`python -m homecareos.retencao.cli`) e
a seção "Retenção e expurgo de dados" do README de apps/api.
"""
