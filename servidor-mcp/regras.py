"""Regras de domínio das salas: politica de uso, conflitos e alternativas.

Nada aqui conhece MCP. O servidor traduz ErroDeRegra em erro de execucao da tool.
"""

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

DADOS = Path(__file__).resolve().parent.parent / "dados"

# A politica fala em horario de Sao Paulo (-03:00), sem horario de verao.
FUSO = timezone(timedelta(hours=-3))
ABERTURA = time(8, 0)
FECHAMENTO = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


class ErroDeRegra(Exception):
    """Violacao de regra de negocio; a mensagem e a definida no enunciado."""


salas: list[dict] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
reservas: list[dict] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))


def versao_da_politica() -> str:
    primeira_linha = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8").splitlines()[0]
    return primeira_linha.split(":", 1)[1].strip()


def texto_da_politica() -> str:
    return (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")


def _instante(valor: str) -> datetime:
    try:
        dt = datetime.fromisoformat(valor)
    except ValueError:
        raise ErroDeRegra(f"Data invalida: {valor} nao e ISO 8601") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=FUSO)


def sala_por_id(sala_id: str) -> dict:
    for s in salas:
        if s["id"] == sala_id:
            return s
    raise ErroDeRegra(f"Sala inexistente: {sala_id}")


def validar(sala_id: str, inicio: str, fim: str) -> tuple[dict, datetime, datetime]:
    """Aplica sala e politica, nessa ordem. Devolve a sala e os instantes ja parseados."""
    sala = sala_por_id(sala_id)
    ini, fi = _instante(inicio), _instante(fim)
    if fi <= ini:
        raise ErroDeRegra(ERRO_INTERVALO)
    ini_local, fi_local = ini.astimezone(FUSO), fi.astimezone(FUSO)
    mesmo_dia = ini_local.date() == fi_local.date()
    if not (mesmo_dia and ini_local.time() >= ABERTURA and fi_local.time() <= FECHAMENTO):
        raise ErroDeRegra(ERRO_JANELA)
    if fi - ini > DURACAO_MAXIMA:
        raise ErroDeRegra(ERRO_DURACAO)
    return sala, ini, fi


def criar_reserva(sala_id: str, inicio: str, fim: str, responsavel: str) -> dict:
    """Grava a reserva em memoria. Quem chama ja validou e verificou conflitos."""
    reserva = {
        "id": f"res-{len(reservas) + 1:04d}",
        "sala": sala_id,
        "inicio": inicio,
        "fim": fim,
        "responsavel": responsavel,
    }
    reservas.append(reserva)
    return reserva


def alternativas(sala_id: str, ini: datetime, fi: datetime, limite: int = 3) -> list[str]:
    """Salas livres no intervalo com capacidade >= a da pedida: menor capacidade primeiro, depois id."""
    pedida = sala_por_id(sala_id)
    candidatas = [
        s
        for s in salas
        if s["id"] != sala_id and s["capacidade"] >= pedida["capacidade"] and not conflitos(s["id"], ini, fi)
    ]
    candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in candidatas[:limite]]


def conflitos(sala_id: str, ini: datetime, fi: datetime) -> list[dict]:
    """Reservas da sala que se sobrepoem ao intervalo [ini, fi)."""
    return [
        r
        for r in reservas
        if r["sala"] == sala_id and _instante(r["inicio"]) < fi and ini < _instante(r["fim"])
    ]
