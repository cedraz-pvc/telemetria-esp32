"""
API que recebe e valida as leituras do ESP32.

Rodar (na raiz do projeto):
    uvicorn backend.app:app --host 0.0.0.0 --port 8000

Documentação interativa: http://localhost:8000/docs
"""
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .modelos import LeituraEntrada, LeituraResposta
from .servico import ErroAutenticacao, ServicoTelemetria

CAMINHO_BANCO_PADRAO = os.getenv("BANCO_PATH", "dados/telemetria.db")
TOKEN_PADRAO = os.getenv("TOKEN_DISPOSITIVO", "troque-este-token")
LIMITE_PADRAO = float(os.getenv("LIMITE_ALARME", "23.0"))   # igual ao LIMITE do main.py


def criar_app(caminho_banco=CAMINHO_BANCO_PADRAO, token_padrao=TOKEN_PADRAO,
              limite_padrao=LIMITE_PADRAO):

    @asynccontextmanager
    async def ciclo_de_vida(app: FastAPI):
        servico = ServicoTelemetria(caminho_banco)
        servico.inicializar(token_padrao=token_padrao, limite_padrao=limite_padrao)
        app.state.servico = servico
        yield

    app = FastAPI(
        title="Telemetria ESP32 — DHT11",
        description="Recebe leituras do ESP32, valida, grava de forma atômica e audita.",
        version="1.0.0",
        lifespan=ciclo_de_vida,
    )

    def ip_de(request: Request):
        return request.client.host if request.client else None

    # ----------------------------------------------- erros de validação -> auditoria
    @app.exception_handler(RequestValidationError)
    async def tratar_validacao(request: Request, exc: RequestValidationError):
        corpo = exc.body if isinstance(exc.body, dict) else {}
        erros = [{"campo": ".".join(str(p) for p in e["loc"][1:]), "erro": e["msg"]}
                 for e in exc.errors()]
        if request.url.path == "/api/leituras":
            request.app.state.servico.registrar_auditoria(
                "REJEITADA_VALIDACAO",
                dispositivo_id=str(corpo.get("dispositivo_id"))[:40] if corpo else None,
                id_transacao=str(corpo.get("id_transacao"))[:80] if corpo else None,
                ip=ip_de(request),
                detalhes={"erros": erros})
        return JSONResponse(status_code=422, content={"status": "rejeitada", "erros": erros})

    # ------------------------------------------------------------- rotas
    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/leituras", response_model=LeituraResposta,
              summary="Recebe uma leitura do ESP32")
    def receber_leitura(leitura: LeituraEntrada, request: Request,
                        x_device_token: Optional[str] = Header(default=None)):
        servico: ServicoTelemetria = request.app.state.servico
        try:
            return servico.registrar_leitura(leitura, x_device_token, ip_de(request))
        except ErroAutenticacao as erro:
            raise HTTPException(status_code=401, detail=str(erro))

    @app.get("/api/saldos/{dispositivo_id}", summary="Saldo consolidado (busca O(1) em memória)")
    def obter_saldo(dispositivo_id: str, request: Request):
        saldo = request.app.state.servico.buscar_saldo(dispositivo_id)
        if saldo is None:
            raise HTTPException(status_code=404, detail="dispositivo não encontrado")
        return saldo

    @app.get("/api/transacoes", summary="Últimas transações gravadas")
    def listar_transacoes(request: Request, dispositivo_id: Optional[str] = None,
                          limite: int = Query(20, ge=1, le=500)):
        return request.app.state.servico.listar_transacoes(dispositivo_id, limite)

    @app.get("/api/auditoria", summary="Trilha de auditoria")
    def listar_auditoria(request: Request, evento: Optional[str] = None,
                         limite: int = Query(50, ge=1, le=500)):
        return request.app.state.servico.listar_auditoria(evento, limite)

    return app


app = criar_app()
