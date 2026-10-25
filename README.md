# A Ponte: um agente A2A com MCP por dentro

Duas pontas de uma fronteira entre protocolos, para as cinco salas da Hill Valley Tech:

```
Cliente A2A ──JSON-RPC/HTTP──▶ agente (:7300) ──JSON-RPC/HTTP──▶ servidor MCP (:7301)
             SendMessage        servidor A2A por fora             3 tools + 1 resource
             GetTask            host MCP por dentro               regras das salas, MRTR
```

- `servidor-mcp/`: servidor MCP (Streamable HTTP, sem sessão) com `listar_salas`, `consultar_disponibilidade`, `reservar_sala` e o resource `politica://uso`. Conflito de horário vira `input_required` (MRTR) com `requestState` selado.
- `agente/`: servidor A2A v1.0 (Agent Card, `SendMessage`, `GetTask`) que, por dentro, é cliente MCP. Sem LLM: o pedido tem formato fixo e a decisão é por regra.

Python 3.10 ou superior. Versões travadas em `servidor-mcp/pyproject.toml` (`mcp==2.3.0`) e `agente/pyproject.toml` (`httpx`, `starlette`, `uvicorn`).

## Como rodar

A partir de um clone limpo, em três terminais.

**Preparação (uma vez):**

```bash
python3 -m venv servidor-mcp/.venv && servidor-mcp/.venv/bin/pip install -e servidor-mcp
python3 -m venv agente/.venv       && agente/.venv/bin/pip install -e agente
```

**Terminal 1: servidor MCP (porta 7301).** A chave que sela o `requestState` vem da variável `REQUEST_STATE_SECRET` (mínimo 32 bytes). Gere a sua e exporte; nunca a escreva no repositório:

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
cd servidor-mcp && .venv/bin/python servidor.py
```

Sem a variável o servidor se recusa a subir. O stderr deste terminal mostra uma linha por request (`metodo`, `id`, `alvo`, `traceparent`).

**Terminal 2: agente (porta 7300):**

```bash
cd agente && .venv/bin/python servidor_a2a.py
```

**Terminal 3: validador** (suba os dois processos do zero antes de cada rodada: as reservas criadas por uma execução mudam o resultado da seguinte):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Variáveis opcionais: `MCP_HOST`/`MCP_PORT` (servidor, padrão `127.0.0.1:7301`), `AGENTE_HOST`/`AGENTE_PORT`/`AGENTE_URL`/`MCP_URL` (agente).

**Conferir a mão** (conflito → pausa → continuação):

```bash
curl -s localhost:7300/a2a -H 'Content-Type: application/json' -H 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01' \
  -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"messageId":"m1","role":"ROLE_USER","parts":[{"text":"reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"}]}}}'
# a Task volta em TASK_STATE_INPUT_REQUIRED com "alternativas: sala-fusca, sala-mirante"
# continuar: mesma chamada com "taskId": "<id da Task>" e o texto "escolha=sala-mirante" (ou "escolha=recusar")
```

## Onde a ponte acontece

O `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED` em [`Ponte._traduzir`](agente/ponte.py#L100): ao receber `resultType: input_required`, o agente não responde por conta própria nem fica esperando. Ele lê o enum da elicitation, guarda na própria Task uma `Pausa` (chave do `inputRequests`, `requestState` opaco, argumentos originais, alternativas) e devolve ao cliente A2A a linha `alternativas: …`. O `requestState` volta para o servidor em [`Ponte.continuar`](agente/ponte.py#L58): quando chega o `SendMessage` com `taskId` e `escolha=…` (chamado em [`servidor_a2a.py`](agente/servidor_a2a.py#L86)), o agente repete a mesma `tools/call` com um `id` JSON-RPC novo (o contador de [`ClienteMCP`](agente/cliente_mcp.py#L56) nunca repete), levando `inputResponses` com a mesma chave e o `requestState` ecoado sem modificação ([`ponte.py:80`](agente/ponte.py#L80)). `escolha=recusar` vira `action: decline` e a Task termina em `TASK_STATE_CANCELED`. A única coisa que atravessa a fronteira é estado nomeado: a pergunta (A2A) e o `requestState` (MCP).

O agente usa um cliente MCP cru em vez do cliente do SDK de propósito: o cliente do SDK responderia a elicitation sozinho por um callback e o ciclo fecharia sem voltar ao cliente A2A.

## Decisões técnicas

**Proteção do `requestState`.** `RequestStateSecurity` do SDK `mcp` 2.3.0 ([`servidor.py:44`](servidor-mcp/servidor.py#L44)): AES-256-GCM (AEAD) com chave derivada por HKDF de `REQUEST_STATE_SECRET`. Assinar e cifrar: o conteúdo não é legível pelo cliente e qualquer alteração é rejeitada com `-32602`. O token é amarrado ao método, aos argumentos e ao nome do servidor, então um retry com argumentos adulterados é rejeitado (o servidor nunca usa os argumentos reenviados no lugar dos selados). A chave tem no mínimo 32 bytes (o SDK recusa menos que isso) e não existe no código.

**Validade.** 600 s (10 minutos), dentro da faixa de 5 a 30 minutos. Expirado ou adulterado: `-32602`.

**Sem estado no servidor MCP entre as rodadas.** O resolver [`escolha_de_sala`](servidor-mcp/servidor.py#L110) recalcula o conflito a cada chamada; o que precisa viajar viaja no `requestState`. Por isso um retry feito depois de reiniciar o processo (com a mesma chave) funciona. As reservas, essas sim, ficam só em memória, como o enunciado permite.

**Estado das Tasks.** Em memória, no processo do agente ([`Armazem`](agente/tarefas.py)), indexado por id. A `Pausa` (com o `requestState`) fica no objeto da Task e nunca é serializada: não aparece no card, nos artifacts nem em nenhuma mensagem. Cada Task tem a sua, então duas Tasks pausadas ao mesmo tempo não trocam de estado. Uma trava por Task serializa mensagens concorrentes para a mesma Task. Estado terminal é definitivo (`Tarefa.transitar` recusa mexer).

**Divisão de responsabilidades.** O agente não implementa regra de sala: conflito, política e alternativas são do servidor. O agente só interpreta o pedido de formato fixo e traduz protocolo.

**`traceparent`.** O header do `SendMessage` é guardado na Task; cada request MCP daquela Task leva o mesmo trace-id e um span-id novo ([`novo_traceparent`](agente/ponte.py#L31)). O servidor MCP registra o valor no stderr.

**Notas sobre o SDK.**
- Erros de execução saem com o prefixo `Error executing tool <nome>:`; o texto exato da regra vem em seguida (aceito pelo enunciado). Tool inexistente volta como `isError: true`, não `-32602` (também aceito).
- Requests sem `_meta` obrigatório são rejeitados pelo transporte (`-32602`, HTTP 400) antes do middleware de log do SDK, então não aparecem no stderr; todos os demais aparecem.
- O resolver precisa ser anotado como `ElicitationResult[T]` exato (com `| None` o SDK aborta a chamada no `decline`) e, sem conflito, devolve o modelo puro, que o SDK embrulha como escolha aceita.

## Saída do validador

```
trace-id desta execucao: fafe500c6d92650dc600d6431ca3f74e
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
