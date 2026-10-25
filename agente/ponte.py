"""A ponte: traduz entre a Task do A2A (fora) e o cliente MCP (dentro).

O agente traduz protocolo, nao dominio: conflito, politica e alternativas sao decisao do
servidor MCP. Aqui so se decide o que fazer com o que o servidor devolveu.
"""

import json
import re
import secrets

import httpx

import tarefas as t
from cliente_mcp import ClienteMCP, ErroMCP

USO = "Pedido invalido: use reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"
CAMPOS = ("sala", "inicio", "fim", "responsavel")
ESCOLHA = re.compile(r"^escolha=(\S+)$")
TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-[0-9a-f]{16}-([0-9a-f]{2})$")


def interpretar_pedido(texto: str) -> dict | None:
    """`reservar sala=<id> inicio=<iso> fim=<iso> responsavel=<nome>`, formato fixo, sem LLM."""
    texto = texto.strip()
    if not texto.startswith("reservar "):
        return None
    campos = dict(re.findall(r"(\w+)=(.*?)(?=\s+\w+=|\s*$)", texto))
    return {c: campos[c] for c in CAMPOS} if all(c in campos for c in CAMPOS) else None


def novo_traceparent(original: str | None) -> str | None:
    """Mantem o trace-id do cliente A2A e gera um span-id novo para cada request MCP."""
    casou = TRACEPARENT.match(original or "")
    return f"00-{casou.group(1)}-{secrets.token_hex(8)}-{casou.group(2)}" if casou else None


def linha_de_alternativas(alternativas: list[str]) -> str:
    return "alternativas: " + ", ".join(alternativas)


def texto_do_resultado(resultado: dict) -> str:
    return " ".join(p.get("text", "") for p in resultado.get("content", []))


class Ponte:
    def __init__(self, cliente: ClienteMCP) -> None:
        self._mcp = cliente

    async def abrir(self, tarefa: t.Tarefa, texto: str) -> None:
        """Primeira mensagem de uma Task: interpreta o pedido e chama o servidor MCP."""
        pedido = interpretar_pedido(texto)
        if pedido is None:
            tarefa.transitar(t.FAILED, USO)
            return
        tarefa.transitar(t.WORKING)
        await self._executar(tarefa, pedido, lambda tp: self._mcp.chamar("reservar_sala", pedido, traceparent=tp))

    async def continuar(self, tarefa: t.Tarefa, texto: str) -> None:
        """Mensagem que retoma uma Task pausada: `escolha=<sala>` ou `escolha=recusar`."""
        pausa = tarefa.pausa
        casou = ESCOLHA.match(texto.strip())
        escolha = casou.group(1) if casou else None
        if escolha is None or (escolha != "recusar" and escolha not in pausa.alternativas):
            # Resposta invalida nao encerra nem retoma: a Task continua pausada e repete a pergunta.
            tarefa.transitar(t.INPUT_REQUIRED, linha_de_alternativas(pausa.alternativas))
            return
        if escolha == "recusar":
            resposta = {"action": "decline"}
        else:
            resposta = {"action": "accept", "content": {"sala": escolha}}
        tarefa.transitar(t.WORKING)
        tarefa.pausa = None

        def retry(tp: str | None):
            # Mesma tools/call de antes (id novo, gerado pelo cliente), agora com a resposta e o
            # requestState que o servidor MCP entregou, devolvido sem modificacao.
            return self._mcp.chamar(
                "reservar_sala",
                pausa.argumentos,
                input_responses={pausa.chave: resposta},
                request_state=pausa.request_state,
                traceparent=tp,
            )

        await self._executar(tarefa, pausa.argumentos, retry)

    async def _executar(self, tarefa: t.Tarefa, pedido: dict, chamada) -> None:
        try:
            await self._mcp.listar_ferramentas(novo_traceparent(tarefa.traceparent))
            resultado = await chamada(novo_traceparent(tarefa.traceparent))
            await self._traduzir(tarefa, pedido, resultado)
        except ErroMCP as e:
            tarefa.transitar(t.FAILED, f"Erro do servidor MCP: {e.mensagem}")
        except httpx.HTTPError:
            tarefa.transitar(t.FAILED, "Servidor MCP indisponivel")

    async def _traduzir(self, tarefa: t.Tarefa, pedido: dict, resultado: dict) -> None:
        """Resultado MCP -> estado da Task."""
        if resultado["resultType"] == "input_required":
            # PONTE (ida): o servidor MCP pediu informacao. A chamada nao fica aberta esperando:
            # a Task e interrompida, o requestState fica guardado nela, e a pergunta vai ao cliente A2A.
            chave, pergunta = next(iter(resultado["inputRequests"].items()))
            campo = pergunta["params"]["requestedSchema"]["properties"]["sala"]
            alternativas = campo.get("enum") or [campo["const"]]
            tarefa.pausa = t.Pausa(
                chave=chave,
                request_state=resultado["requestState"],
                argumentos=pedido,
                alternativas=alternativas,
            )
            tarefa.transitar(t.INPUT_REQUIRED, linha_de_alternativas(alternativas))
            return
        if resultado.get("isError"):
            # Erro de execucao da tool: a mensagem exata dela chega ao cliente A2A.
            tarefa.transitar(t.FAILED, texto_do_resultado(resultado))
            return
        dados = resultado.get("structuredContent") or {}
        if dados.get("reservado") is False:
            tarefa.transitar(t.CANCELED, "Reserva recusada.")
            return
        politica = await self._mcp.versao_da_politica(novo_traceparent(tarefa.traceparent))
        reserva = {
            "reserva": dados["reserva"],
            "sala": dados["sala"],
            "inicio": dados["inicio"],
            "fim": dados["fim"],
            "responsavel": dados["responsavel"],
            "politica": politica,
        }
        tarefa.artefatos.append(
            {"artifactId": t.novo_id("art"), "name": "reserva", "parts": [{"text": json.dumps(reserva)}]}
        )
        tarefa.transitar(t.COMPLETED, f"Reserva {dados['reserva']} confirmada na {dados['sala']}.")
