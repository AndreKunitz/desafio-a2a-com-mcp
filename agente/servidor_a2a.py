"""O agente como servidor A2A v1.0 (binding JSON-RPC sobre HTTP).

Expoe o Agent Card, SendMessage e GetTask. Sem streaming, sem autenticacao.
"""

import os

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import tarefas as t
from cliente_mcp import ClienteMCP
from ponte import Ponte

PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENTE_URL", f"http://127.0.0.1:{PORTA}")
URL_MCP = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

# Codigos de erro do A2A v1.0.
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
TASK_NOT_FOUND, UNSUPPORTED_OPERATION = -32001, -32004

AGENT_CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [{"url": f"{URL_PUBLICA}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}

armazem = t.Armazem()
ponte = Ponte(ClienteMCP(URL_MCP))


class ErroRpc(Exception):
    def __init__(self, codigo: int, mensagem: str) -> None:
        self.codigo, self.mensagem = codigo, mensagem


def texto_da_mensagem(mensagem: dict) -> str:
    return " ".join(p.get("text", "") for p in mensagem.get("parts", []) if isinstance(p, dict))


async def send_message(params: dict, traceparent: str | None) -> dict:
    mensagem = params.get("message")
    if not isinstance(mensagem, dict) or not isinstance(mensagem.get("parts"), list):
        raise ErroRpc(INVALID_PARAMS, "message com parts e obrigatoria")
    texto = texto_da_mensagem(mensagem)

    task_id = mensagem.get("taskId")
    if task_id is None:
        tarefa = armazem.criar(traceparent)
        tarefa.registrar_usuario(mensagem)
        await ponte.abrir(tarefa, texto)
        return {"task": tarefa.para_a2a()}

    tarefa = armazem.obter(task_id)
    if tarefa is None:
        raise ErroRpc(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
    async with tarefa.trava:
        if tarefa.estado in t.TERMINAIS:
            raise ErroRpc(UNSUPPORTED_OPERATION, f"Task {task_id} ja terminou em {tarefa.estado}")
        if tarefa.estado != t.INPUT_REQUIRED:
            raise ErroRpc(UNSUPPORTED_OPERATION, f"Task {task_id} nao aguarda mensagem")
        tarefa.traceparent = tarefa.traceparent or traceparent  # o trace-id da Task nao muda
        tarefa.registrar_usuario(mensagem)
        # PONTE (volta): a resposta do cliente A2A retoma o request MCP original.
        await ponte.continuar(tarefa, texto)
    return {"task": tarefa.para_a2a()}


async def get_task(params: dict, traceparent: str | None) -> dict:
    tarefa = armazem.obter(str(params.get("id", "")))
    if tarefa is None:
        raise ErroRpc(TASK_NOT_FOUND, f"Task nao encontrada: {params.get('id')}")
    return {"task": tarefa.para_a2a()}


METODOS = {"SendMessage": send_message, "GetTask": get_task}


async def endpoint_a2a(request: Request) -> JSONResponse:
    try:
        corpo = await request.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "JSON invalido"}})
    id_ = corpo.get("id") if isinstance(corpo, dict) else None
    try:
        if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0":
            raise ErroRpc(INVALID_REQUEST, "Requisicao JSON-RPC 2.0 invalida")
        metodo = METODOS.get(corpo.get("method"))
        if metodo is None:
            raise ErroRpc(METHOD_NOT_FOUND, f"Metodo nao suportado: {corpo.get('method')}")
        params = corpo.get("params")
        if not isinstance(params, dict):
            raise ErroRpc(INVALID_PARAMS, "params deve ser um objeto")
        resultado = await metodo(params, request.headers.get("traceparent"))
        return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": resultado})
    except ErroRpc as e:
        return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": e.codigo, "message": e.mensagem}})


async def agent_card(request: Request) -> JSONResponse:
    return JSONResponse(AGENT_CARD)


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
        Route("/a2a", endpoint_a2a, methods=["POST"]),
    ]
)


def main() -> None:
    uvicorn.run(app, host=os.environ.get("AGENTE_HOST", "127.0.0.1"), port=PORTA)


if __name__ == "__main__":
    main()
