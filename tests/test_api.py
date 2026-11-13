"""Testes da API: validação, autenticação, idempotência, saldo e atomicidade.
Rodar na raiz do projeto:  pytest -v
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.app import criar_app
from backend.tabela_hash import TabelaHash

TOKEN = "token-de-teste"
CAB = {"X-Device-Token": TOKEN}


def leitura(seq=1, temp=24.0, media=None, alarme=False, **extra):
    corpo = {
        "dispositivo_id": "esp32-01",
        "id_transacao": "esp32-01-a1b2c3d4-{:06d}".format(seq),
        "seq": seq,
        "temperatura": temp,
        "umidade": 60.0,
        "media_movel": temp if media is None else media,
        "alarme": alarme,
    }
    corpo.update(extra)
    return corpo


@pytest.fixture
def cliente(tmp_path):
    banco = tmp_path / "teste.db"
    with TestClient(criar_app(str(banco), TOKEN)) as c:
        c.banco = banco
        yield c


def contar(banco, tabela, onde="1=1"):
    con = sqlite3.connect(banco)
    try:
        return con.execute("SELECT COUNT(*) FROM {} WHERE {}".format(tabela, onde)).fetchone()[0]
    finally:
        con.close()


# ------------------------------------------------------------- tabela hash
def test_tabela_hash_basico_e_rehash():
    t = TabelaHash(capacidade_inicial=8)
    for i in range(100):
        t.inserir("k{}".format(i), i)
    assert len(t) == 100
    assert t.capacidade > 8 and t.fator_carga <= 0.75
    assert t.buscar("k42") == 42 and "k99" in t
    t.inserir("k42", "novo")
    assert t.buscar("k42") == "novo" and len(t) == 100
    assert t.remover("k42") and t.buscar("k42") is None and len(t) == 99


# ------------------------------------------------------------------- API
def test_leitura_aceita_atualiza_saldo(cliente):
    r = cliente.post("/api/leituras", json=leitura(1, 24.0), headers=CAB)
    assert r.status_code == 200 and r.json()["status"] == "aceita"
    cliente.post("/api/leituras", json=leitura(2, 26.0), headers=CAB)
    saldo = cliente.get("/api/saldos/esp32-01").json()
    assert saldo["total_leituras"] == 2
    assert saldo["media_geral"] == 25.0
    assert saldo["temp_min"] == 24.0 and saldo["temp_max"] == 26.0


def test_reenvio_e_idempotente(cliente):
    cliente.post("/api/leituras", json=leitura(1), headers=CAB)
    r = cliente.post("/api/leituras", json=leitura(1), headers=CAB)
    assert r.status_code == 200 and r.json()["status"] == "duplicada"
    assert contar(cliente.banco, "transacoes") == 1
    assert cliente.get("/api/saldos/esp32-01").json()["total_leituras"] == 1
    assert contar(cliente.banco, "logs_auditoria", "evento='TRANSACAO_DUPLICADA'") == 1


def test_token_invalido_401_e_auditado(cliente):
    r = cliente.post("/api/leituras", json=leitura(), headers={"X-Device-Token": "errado"})
    assert r.status_code == 401
    r = cliente.post("/api/leituras", json=leitura())   # sem token
    assert r.status_code == 401
    assert contar(cliente.banco, "transacoes") == 0
    assert contar(cliente.banco, "logs_auditoria", "evento='REJEITADA_AUTENTICACAO'") == 2


@pytest.mark.parametrize("corpo", [
    leitura(temp=80.0),                              # fora da faixa do DHT11
    leitura(umidade=2.0),
    leitura(alarme="sim"),                           # bool estrito
    leitura(campo_estranho=1),                       # campo extra
    leitura(id_transacao="outro-disp-0001"),         # id não pertence ao dispositivo
    {"dispositivo_id": "esp32-01"},                  # campos faltando
])
def test_validacao_422_e_auditada(cliente, corpo):
    r = cliente.post("/api/leituras", json=corpo, headers=CAB)
    assert r.status_code == 422 and r.json()["status"] == "rejeitada"
    assert contar(cliente.banco, "transacoes") == 0
    assert contar(cliente.banco, "logs_auditoria", "evento='REJEITADA_VALIDACAO'") == 1


def test_alarme_com_histerese(cliente):
    # limite 23.0 °C, histerese 0.5 °C -> desliga abaixo de 22.5 °C
    cliente.post("/api/leituras", json=leitura(1, 22.0), headers=CAB)
    r = cliente.post("/api/leituras", json=leitura(2, 23.4, alarme=True), headers=CAB)
    assert r.json()["alarme_servidor"] is True
    r = cliente.post("/api/leituras", json=leitura(3, 22.7, alarme=True), headers=CAB)
    assert r.json()["alarme_servidor"] is True       # dentro da histerese: continua ligado
    r = cliente.post("/api/leituras", json=leitura(4, 22.0), headers=CAB)
    assert r.json()["alarme_servidor"] is False
    assert contar(cliente.banco, "logs_auditoria", "evento='ALARME_LIGADO'") == 1
    assert contar(cliente.banco, "logs_auditoria", "evento='ALARME_DESLIGADO'") == 1
    assert cliente.get("/api/saldos/esp32-01").json()["total_alarmes"] == 1


def test_atomicidade_rollback_total(cliente):
    """Se o UPDATE do saldo falhar, a transação inserida antes também é desfeita."""
    cliente.post("/api/leituras", json=leitura(1), headers=CAB)
    con = sqlite3.connect(cliente.banco)
    con.execute("""CREATE TRIGGER falha_saldo BEFORE UPDATE ON saldos
                   BEGIN SELECT RAISE(ABORT, 'falha simulada'); END""")
    con.commit()
    con.close()

    sem_erro = TestClient(cliente.app, raise_server_exceptions=False)
    r = sem_erro.post("/api/leituras", json=leitura(2), headers=CAB)
    assert r.status_code == 500
    assert contar(cliente.banco, "transacoes") == 1              # a 2ª NÃO ficou gravada
    assert contar(cliente.banco, "logs_auditoria", "evento='TRANSACAO_ACEITA'") == 1
    assert contar(cliente.banco, "logs_auditoria", "evento='ERRO_INTERNO'") == 1
    assert cliente.get("/api/saldos/esp32-01").json()["total_leituras"] == 1


def test_logs_sao_imutaveis(cliente):
    cliente.post("/api/leituras", json=leitura(1), headers=CAB)
    con = sqlite3.connect(cliente.banco)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM logs_auditoria")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE transacoes SET temperatura = 0")
    con.close()


def test_duplicada_detectada_pelo_banco_apos_reinicio(tmp_path):
    """Servidor reiniciou (cache de ids vazio): o UNIQUE do banco ainda barra o reenvio."""
    banco = str(tmp_path / "reinicio.db")
    with TestClient(criar_app(banco, TOKEN)) as c:
        c.post("/api/leituras", json=leitura(1), headers=CAB)
    with TestClient(criar_app(banco, TOKEN)) as c:     # "novo processo"
        r = c.post("/api/leituras", json=leitura(1), headers=CAB)
        assert r.json()["status"] == "duplicada"
        assert c.get("/api/saldos/esp32-01").json()["total_leituras"] == 1
