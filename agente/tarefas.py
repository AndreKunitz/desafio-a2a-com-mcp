"""A Task do A2A: identidade, estado e produto.

No A2A o estado vive na Task (tem id), nao numa sessao. Estado terminal e definitivo;
INPUT_REQUIRED e um estado interrompido, que uma nova mensagem pode retomar.
"""

import asyncio
import secrets
from dataclasses import dataclass, field

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
TERMINAIS = {COMPLETED, CANCELED, FAILED}


def novo_id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


class EstadoTerminal(Exception):
    """Tentativa de mexer em uma Task que ja terminou."""


@dataclass
class Tarefa:
    id: str
    context_id: str
    traceparent: str | None = None
    estado: str = SUBMITTED
    mensagem_de_status: dict | None = None
    historico: list[dict] = field(default_factory=list)
    artefatos: list[dict] = field(default_factory=list)
    # Estado da ponte: fica no agente, ligado a esta Task, e nunca e serializado para o cliente.
    pausa: "Pausa | None" = None
    # Serializa mensagens concorrentes para a mesma Task (duas continuacoes nao podem correr juntas).
    trava: asyncio.Lock = field(default_factory=asyncio.Lock)

    def registrar_usuario(self, mensagem: dict) -> None:
        self.historico.append(mensagem)

    def transitar(self, estado: str, texto: str | None = None) -> None:
        if self.estado in TERMINAIS:
            raise EstadoTerminal(self.id)
        self.estado = estado
        self.mensagem_de_status = None
        if texto is not None:
            self.mensagem_de_status = {
                "messageId": novo_id("msg"),
                "role": "ROLE_AGENT",
                "parts": [{"text": texto}],
                "taskId": self.id,
                "contextId": self.context_id,
            }
            self.historico.append(self.mensagem_de_status)

    def para_a2a(self) -> dict:
        status: dict = {"state": self.estado}
        if self.mensagem_de_status:
            status["message"] = self.mensagem_de_status
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": self.historico,
            "artifacts": self.artefatos,
        }


@dataclass
class Pausa:
    """O que o agente guarda de uma Task interrompida para poder retomar o request MCP."""

    chave: str  # chave do inputRequests, atribuida pelo servidor MCP
    request_state: str  # opaco: guardado e ecoado, nunca aberto
    argumentos: dict  # os argumentos originais da tools/call, para repeti-la
    alternativas: list[str]


class Armazem:
    """Tasks em memoria, por id. Cada Task carrega a sua propria pausa."""

    def __init__(self) -> None:
        self._tarefas: dict[str, Tarefa] = {}

    def criar(self, traceparent: str | None) -> Tarefa:
        tarefa = Tarefa(id=novo_id("task"), context_id=novo_id("ctx"), traceparent=traceparent)
        self._tarefas[tarefa.id] = tarefa
        return tarefa

    def obter(self, task_id: str) -> Tarefa | None:
        return self._tarefas.get(task_id)
