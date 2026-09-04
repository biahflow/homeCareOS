"""Rate limit das rotas caras, por identidade do principal — ADR 0005 (issue #39).

Quatro rotas de `/api/*` custam desproporcionalmente mais que as outras 22 —
uma delas custa **dinheiro** (`POST /api/documentos` chama o provider de IA
dentro da requisição) — e são as únicas que este pacote freia. As leituras
paginadas continuam sem limite: cobrar de todas o preço de proteger quatro
seria custo certo contra risco hipotético.

Três decisões do ADR moram aqui:

- **A chave é a identidade, nunca o IP.** Atrás do proxy do projeto
  (`confiar_em_x_forwarded_for` tem default `false`), limitar por IP ou tranca a
  equipe inteira num contador só, ou — com a flag ligada, sem allowlist de proxy
  — deixa qualquer cliente forjar um IP novo por requisição. Quem chega nestas
  quatro rotas já tem identidade verificada: `usuario_id` para pessoa,
  `"api"` para a chave de máquina. Ver `protecao.chave_do_principal`.
- **O contador vive no Postgres**, não em memória do processo: um contador em
  memória não quebra quando alguém acrescentar uma réplica da API — ele **dobra
  o limite em silêncio**, sem erro, sem teste vermelho e sem rastro.
- **Dependency por rota, não middleware.** Um middleware forçaria uma chave e um
  limite únicos para rotas que não têm nada em comum além do prefixo da URL.
  Ver `dependencies.limitar`.

Isto **não** é proteção contra DDoS, e não deve ser vendido como tal: ataque
volumétrico chega antes da aplicação e é trabalho da borda. O que este pacote
contém é abuso de uso legítimo — script mal escrito, integração em laço,
curiosidade cara.
"""
