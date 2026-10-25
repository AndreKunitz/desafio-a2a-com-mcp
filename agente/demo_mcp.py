"""Demonstracao do ciclo de MRTR sem A2A: responde a elicitation "na mao".

Uso (servidor MCP no ar): python demo_mcp.py [sala] [inicio] [fim] [responsavel]
"""

import asyncio
import json
import os
import sys

from cliente_mcp import ClienteMCP

PADRAO = ["sala-garagem", "2026-11-03T14:00:00-03:00", "2026-11-03T15:00:00-03:00", "Marty"]


async def main() -> None:
    sala, inicio, fim, responsavel = (sys.argv[1:] + PADRAO[len(sys.argv) - 1 :])[:4]
    cliente = ClienteMCP(os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp"))
    try:
        print("ferramentas:", sorted(await cliente.listar_ferramentas()))
        print("politica:", await cliente.versao_da_politica())

        argumentos = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
        resultado = await cliente.chamar("reservar_sala", argumentos)
        if resultado["resultType"] == "input_required":
            chave, pedido = next(iter(resultado["inputRequests"].items()))
            enum = pedido["params"]["requestedSchema"]["properties"]["sala"]["enum"]
            print(f"input_required: chave={chave} alternativas={enum}")
            # Aqui o agente A2A pausaria a Task e perguntaria ao cliente. A demo escolhe a primeira.
            resultado = await cliente.chamar(
                "reservar_sala",
                argumentos,
                input_responses={chave: {"action": "accept", "content": {"sala": enum[0]}}},
                request_state=resultado["requestState"],
            )
        print(json.dumps(resultado.get("structuredContent") or resultado["content"], ensure_ascii=False, indent=2))
    finally:
        await cliente.fechar()


if __name__ == "__main__":
    asyncio.run(main())
