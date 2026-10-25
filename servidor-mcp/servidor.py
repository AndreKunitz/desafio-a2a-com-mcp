"""Servidor MCP da Central de Salas (camada de protocolo; as regras vivem em regras.py)."""

import os
import sys
from typing import Annotated, Literal

from mcp.server.mcpserver import Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.request_state import RequestStateSecurity
from pydantic import BaseModel, create_model

import regras

# O requestState volta pelas maos do cliente, entao e selado (AES-GCM) com uma chave
# que so o servidor conhece. Vale 10 minutos (o enunciado pede de 5 a 30).
SEGREDO = os.environ.get("REQUEST_STATE_SECRET")
if not SEGREDO:
    sys.exit(
        "REQUEST_STATE_SECRET nao definida. Gere uma com:\n"
        '  export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")'
    )



async def registrar_request(ctx, call_next):
    """Middleware: uma linha no stderr por request, para depurar a ponte.

    stderr e o canal certo (o logging do protocolo esta depreciado); o stdout fica livre.
    """
    params = ctx.params or {}
    meta = params.get("_meta") or {}
    alvo = params.get("name") or params.get("uri") or "-"
    print(
        f"[mcp] metodo={ctx.method} id={ctx.request_id} alvo={alvo} traceparent={meta.get('traceparent', '-')}",
        file=sys.stderr,
        flush=True,
    )
    return await call_next(ctx)


mcp = MCPServer(
    "central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[SEGREDO], ttl=600),
    middleware=[registrar_request],
)


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


@mcp.tool()
def listar_salas() -> ListaDeSalas:
    """Lista todas as salas com capacidade e recursos."""
    return ListaDeSalas(salas=[SalaOut(**s) for s in regras.salas])


@mcp.tool()
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    """Diz se uma sala esta livre no intervalo, e quais reservas conflitam."""
    try:
        _, ini, fi = regras.validar(sala, inicio, fim)
    except regras.ErroDeRegra as e:
        # ToolError vira resultado complete com isError: true (erro de execucao, nao de protocolo).
        raise ToolError(str(e)) from None
    achados = regras.conflitos(sala, ini, fi)
    return Disponibilidade(
        sala=sala,
        livre=not achados,
        conflitos=[ConflitoOut(**{k: r[k] for k in ConflitoOut.model_fields}) for r in achados],
    )


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class EscolhaDeSala(BaseModel):
    sala: str


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit | EscolhaDeSala:
    """Resolver: decide se a reserva precisa perguntar algo antes de executar.

    Sem conflito devolve a propria sala pedida (o SDK a embrulha como escolha aceita) e a tool segue. Com conflito devolve um Elicit, que o SDK
    transforma em resultType=input_required (e em -32021 se o cliente nao declarou a
    capability de elicitation em form mode). O servidor nao guarda nada entre as duas
    rodadas: ele recalcula o conflito a cada chamada.
    """
    try:
        _, ini, fi = regras.validar(sala, inicio, fim)
    except regras.ErroDeRegra as e:
        raise ToolError(str(e)) from None
    if not regras.conflitos(sala, ini, fi):
        return EscolhaDeSala(sala=sala)
    opcoes = regras.alternativas(sala, ini, fi)
    if not opcoes:
        raise ToolError(regras.ERRO_SEM_ALTERNATIVA)
    # O enum depende do conflito, entao o schema e montado a cada chamada.
    schema = create_model("EscolhaDeSala", __base__=EscolhaDeSala, sala=(Literal[tuple(opcoes)], ...))
    return Elicit("A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", schema)


@mcp.tool()
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[EscolhaDeSala], Resolve(escolha_de_sala)],
) -> ReservaOut:
    """Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar."""
    if escolha.action != "accept":
        return ReservaOut(reservado=False, motivo="recusado")
    sala = escolha.data.sala
    try:
        regras.validar(sala, inicio, fim)
    except regras.ErroDeRegra as e:
        raise ToolError(str(e)) from None
    criada = regras.criar_reserva(sala, inicio, fim, responsavel)
    return ReservaOut(
        reserva=criada["id"],
        sala=sala,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=regras.versao_da_politica(),
    )


@mcp.resource("politica://uso", mime_type="text/markdown")
def politica_de_uso() -> str:
    """Politica de uso das salas."""
    return regras.texto_da_politica()


def main() -> None:
    mcp.run(
        "streamable-http",
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_PORT", "7301")),
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
