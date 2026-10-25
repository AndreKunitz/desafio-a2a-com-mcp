"""Cliente MCP cru (JSON-RPC sobre HTTP) usado pelo agente.

Por que nao o cliente do SDK: ele resolve o input_required sozinho por um callback de
elicitation, e o ciclo fecharia sem nunca voltar para o cliente A2A. Aqui o agente enxerga
o input_required como ele vem e decide o que fazer.

O agente traduz protocolo, nao domínio: nada aqui conhece regra de sala.
"""

import itertools

import httpx

PROTOCOLO = "2026-07-28"
CAPABILITIES = {"elicitation": {"form": {}}}
CLIENT_INFO = {"name": "agente-central-de-salas", "version": "1.0.0"}


class ErroMCP(Exception):
    """Erro de protocolo (campo `error` do JSON-RPC)."""

    def __init__(self, codigo: int, mensagem: str, dados: dict | None = None) -> None:
        super().__init__(f"MCP {codigo}: {mensagem}")
        self.codigo, self.mensagem, self.dados = codigo, mensagem, dados


class ClienteMCP:
    def __init__(self, url: str) -> None:
        self._url = url
        self._http = httpx.AsyncClient(timeout=30)
        # Cada request leva um id novo, inclusive o retry de um input_required.
        self._ids = itertools.count(1)
        self._ferramentas: dict[str, dict] | None = None

    async def fechar(self) -> None:
        await self._http.aclose()

    async def _request(self, metodo: str, params: dict, nome: str | None, traceparent: str | None) -> dict:
        # Nada de sessao: todo request declara versao e capabilities por conta propria.
        meta = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
            "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
            "io.modelcontextprotocol/clientCapabilities": CAPABILITIES,
        }
        if traceparent:
            meta["traceparent"] = traceparent
        # O transporte espelha metodo e nome do corpo em headers; se divergirem, -32020.
        cabecalhos = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOLO,
            "Mcp-Method": metodo,
        }
        if nome:
            cabecalhos["Mcp-Name"] = nome
        corpo = {"jsonrpc": "2.0", "id": next(self._ids), "method": metodo, "params": {**params, "_meta": meta}}
        resposta = (await self._http.post(self._url, json=corpo, headers=cabecalhos)).json()
        if "error" in resposta:
            erro = resposta["error"]
            raise ErroMCP(erro["code"], erro["message"], erro.get("data"))
        return resposta["result"]

    async def listar_ferramentas(self, traceparent: str | None = None) -> dict[str, dict]:
        """tools/list, feito uma vez antes da primeira chamada; o agente nao carrega lista fixa."""
        if self._ferramentas is None:
            resultado = await self._request("tools/list", {}, None, traceparent)
            self._ferramentas = {t["name"]: t for t in resultado["tools"]}
        return self._ferramentas

    async def chamar(
        self,
        ferramenta: str,
        argumentos: dict,
        *,
        input_responses: dict | None = None,
        request_state: str | None = None,
        traceparent: str | None = None,
    ) -> dict:
        """tools/call. Devolve o resultado cru: complete ou input_required."""
        if ferramenta not in await self.listar_ferramentas(traceparent):
            raise ErroMCP(-32602, f"ferramenta nao anunciada pelo servidor: {ferramenta}")
        params: dict = {"name": ferramenta, "arguments": argumentos}
        if input_responses is not None:
            params["inputResponses"] = input_responses
        if request_state is not None:
            params["requestState"] = request_state  # opaco: guardado e ecoado, nunca aberto
        return await self._request("tools/call", params, ferramenta, traceparent)

    async def versao_da_politica(self, traceparent: str | None = None) -> str:
        """Le o resource politica://uso e extrai a versao declarada na primeira linha."""
        resultado = await self._request("resources/read", {"uri": "politica://uso"}, "politica://uso", traceparent)
        primeira_linha = resultado["contents"][0]["text"].splitlines()[0]
        return primeira_linha.split(":", 1)[1].strip()
