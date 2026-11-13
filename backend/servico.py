"""
Lógica de negócio do backend.

Estruturas em memória (todas TabelaHash -> buscas O(1)):
    _dispositivos    id do dispositivo -> configuração (token, limite...)  [autenticação]
    _ids_processados id_transacao      -> resposta já enviada              [idempotência]
    _saldos          id do dispositivo -> saldo consolidado                [consulta rápida]

Persistência (SQLite):
    Cada leitura é gravada numa ÚNICA transação de banco (BEGIN IMMEDIATE):
        1. INSERT em transacoes
        2. UPDATE em saldos (incrementos calculados dentro do próprio SQL)
        3. INSERT em logs_auditoria (aceite + eventual mudança de alarme)
    Se qualquer passo falhar -> ROLLBACK de tudo. Nunca fica transação
    gravada sem saldo atualizado, nem saldo atualizado sem transação.
    Os caches em memória só são atualizados DEPOIS do COMMIT.
"""
import hashlib
import hmac
import json
import sqlite3
import threading
from pathlib import Path

from .tabela_hash import TabelaHash

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"
MAX_IDS_EM_CACHE = 10_000


class ErroAutenticacao(Exception):
    pass


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ServicoTelemetria:
    def __init__(self, caminho_banco):
        self.caminho_banco = str(caminho_banco)
        self._dispositivos = TabelaHash()
        self._ids_processados = TabelaHash(capacidade_inicial=1024)
        self._saldos = TabelaHash()
        # Serializa as escritas entre as threads do servidor. O BEGIN IMMEDIATE
        # protege também contra outro processo escrevendo no mesmo arquivo.
        self._trava = threading.Lock()

    # ================================================================ banco
    def _conectar(self):
        # isolation_level=None -> nós controlamos BEGIN/COMMIT/ROLLBACK manualmente
        con = sqlite3.connect(self.caminho_banco, isolation_level=None, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def inicializar(self, token_padrao=None, limite_padrao=23.0):
        """Cria as tabelas, cadastra o dispositivo de demonstração e carrega os caches."""
        Path(self.caminho_banco).parent.mkdir(parents=True, exist_ok=True)
        con = self._conectar()
        try:
            con.execute("PRAGMA journal_mode = WAL")
            con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            vazio = con.execute("SELECT COUNT(*) FROM dispositivos").fetchone()[0] == 0
        finally:
            con.close()

        if vazio and token_padrao:
            self.cadastrar_dispositivo("esp32-01", "ESP32 + DHT11 + buzzer (bancada)", token_padrao,
                                       limite_alarme=limite_padrao)

        self._carregar_caches()

    def _carregar_caches(self):
        con = self._conectar()
        try:
            self._dispositivos.limpar()
            for linha in con.execute("SELECT * FROM dispositivos"):
                self._dispositivos.inserir(linha["id"], dict(linha))
            self._saldos.limpar()
            for linha in con.execute("SELECT * FROM saldos"):
                self._saldos.inserir(linha["dispositivo_id"], self._formatar_saldo(linha))
        finally:
            con.close()

    def cadastrar_dispositivo(self, dispositivo_id, descricao, token,
                              limite_alarme=23.0, histerese=0.5):
        con = self._conectar()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO dispositivos (id, descricao, token_hash, limite_alarme, histerese) "
                "VALUES (?, ?, ?, ?, ?)",
                (dispositivo_id, descricao, hash_token(token), limite_alarme, histerese))
            con.execute("INSERT INTO saldos (dispositivo_id) VALUES (?)", (dispositivo_id,))
            self._log(con, "DISPOSITIVO_CADASTRADO", dispositivo_id=dispositivo_id,
                      detalhes={"descricao": descricao, "limite_alarme": limite_alarme})
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()
        self._carregar_caches()

    # ============================================================ auditoria
    @staticmethod
    def _log(con, evento, dispositivo_id=None, id_transacao=None, ip=None, detalhes=None):
        con.execute(
            "INSERT INTO logs_auditoria (evento, dispositivo_id, id_transacao, ip_origem, detalhes) "
            "VALUES (?, ?, ?, ?, ?)",
            (evento, dispositivo_id, id_transacao, ip,
             json.dumps(detalhes, ensure_ascii=False) if detalhes is not None else None))

    def registrar_auditoria(self, evento, **kwargs):
        """Log avulso (fora de uma transação de negócio), ex.: requisição rejeitada."""
        con = self._conectar()
        try:
            self._log(con, evento, **kwargs)   # autocommit
        finally:
            con.close()

    # ============================================================== negócio
    def autenticar(self, dispositivo_id, token, ip=None, id_transacao=None):
        """Busca O(1) na tabela hash de dispositivos + comparação em tempo constante."""
        disp = self._dispositivos.buscar(dispositivo_id)
        token_ok = (disp is not None and token is not None
                    and hmac.compare_digest(disp["token_hash"], hash_token(token)))
        if not token_ok or not disp["ativo"]:
            motivo = ("dispositivo desconhecido" if disp is None
                      else "dispositivo inativo" if not disp["ativo"] else "token inválido")
            self.registrar_auditoria("REJEITADA_AUTENTICACAO", dispositivo_id=dispositivo_id,
                                     id_transacao=id_transacao, ip=ip,
                                     detalhes={"motivo": motivo})
            raise ErroAutenticacao(motivo)
        return disp

    def registrar_leitura(self, leitura, token, ip=None):
        """
        Fluxo completo de uma leitura vinda do ESP32.
        `leitura` já chega validada pelo Pydantic (modelos.LeituraEntrada).
        """
        disp = self.autenticar(leitura.dispositivo_id, token, ip, leitura.id_transacao)

        with self._trava:
            # 1) Idempotência: o ESP32 reenviou algo que já processamos? -> O(1)
            resposta_anterior = self._ids_processados.buscar(leitura.id_transacao)
            if resposta_anterior is not None:
                self.registrar_auditoria("TRANSACAO_DUPLICADA", dispositivo_id=disp["id"],
                                         id_transacao=leitura.id_transacao, ip=ip,
                                         detalhes={"origem": "cache"})
                return {**resposta_anterior, "status": "duplicada"}

            # 2) Escrita atômica
            con = self._conectar()
            try:
                con.execute("BEGIN IMMEDIATE")   # trava de escrita já no início

                saldo_atual = con.execute(
                    "SELECT alarme_ativo FROM saldos WHERE dispositivo_id = ?",
                    (disp["id"],)).fetchone()
                alarme_antes = bool(saldo_atual["alarme_ativo"])

                # Alarme recalculado no servidor (mesma regra de histerese da placa)
                if not alarme_antes and leitura.media_movel >= disp["limite_alarme"]:
                    alarme_depois = True
                elif alarme_antes and leitura.media_movel < disp["limite_alarme"] - disp["histerese"]:
                    alarme_depois = False
                else:
                    alarme_depois = alarme_antes

                try:
                    con.execute(
                        "INSERT INTO transacoes (id_transacao, dispositivo_id, seq, temperatura, "
                        "umidade, media_movel, alarme_esp32, alarme_servidor) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (leitura.id_transacao, disp["id"], leitura.seq, leitura.temperatura,
                         leitura.umidade, leitura.media_movel, int(leitura.alarme),
                         int(alarme_depois)))
                except sqlite3.IntegrityError as erro:
                    if "id_transacao" not in str(erro):
                        raise
                    # Já estava no banco (ex.: servidor reiniciou e o cache foi zerado)
                    con.execute("ROLLBACK")
                    self.registrar_auditoria("TRANSACAO_DUPLICADA", dispositivo_id=disp["id"],
                                             id_transacao=leitura.id_transacao, ip=ip,
                                             detalhes={"origem": "banco"})
                    return self._resposta_de_transacao_existente(leitura.id_transacao)

                # Atualização atômica do saldo: os incrementos são feitos pelo próprio
                # SQLite (campo = campo + ?), sem ler-modificar-gravar em Python.
                con.execute(
                    """
                    UPDATE saldos SET
                        total_leituras     = total_leituras + 1,
                        soma_temperatura   = soma_temperatura + :t,
                        temp_min           = MIN(COALESCE(temp_min, :t), :t),
                        temp_max           = MAX(COALESCE(temp_max, :t), :t),
                        ultima_temperatura = :t,
                        ultima_umidade     = :u,
                        alarme_ativo       = :alarme,
                        total_alarmes      = total_alarmes + :novo_alarme,
                        versao             = versao + 1,
                        atualizado_em      = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE dispositivo_id = :d
                    """,
                    {"t": leitura.temperatura, "u": leitura.umidade,
                     "alarme": int(alarme_depois),
                     "novo_alarme": int(alarme_depois and not alarme_antes),
                     "d": disp["id"]})

                self._log(con, "TRANSACAO_ACEITA", dispositivo_id=disp["id"],
                          id_transacao=leitura.id_transacao, ip=ip,
                          detalhes={"seq": leitura.seq, "temperatura": leitura.temperatura,
                                    "umidade": leitura.umidade, "media_movel": leitura.media_movel})
                if alarme_depois != alarme_antes:
                    self._log(con, "ALARME_LIGADO" if alarme_depois else "ALARME_DESLIGADO",
                              dispositivo_id=disp["id"], id_transacao=leitura.id_transacao, ip=ip,
                              detalhes={"media_movel": leitura.media_movel,
                                        "limite": disp["limite_alarme"]})
                if leitura.alarme != alarme_depois:
                    # Placa e servidor discordam: não bloqueia, mas fica registrado
                    self._log(con, "DIVERGENCIA_ALARME", dispositivo_id=disp["id"],
                              id_transacao=leitura.id_transacao, ip=ip,
                              detalhes={"alarme_esp32": leitura.alarme,
                                        "alarme_servidor": alarme_depois})

                saldo_novo = con.execute(
                    "SELECT * FROM saldos WHERE dispositivo_id = ?", (disp["id"],)).fetchone()

                con.execute("COMMIT")
            except Exception as erro:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                self.registrar_auditoria("ERRO_INTERNO", dispositivo_id=disp["id"],
                                         id_transacao=leitura.id_transacao, ip=ip,
                                         detalhes={"erro": repr(erro)})
                raise
            finally:
                con.close()

            # 3) Só depois do COMMIT os caches refletem o novo estado
            saldo_fmt = self._formatar_saldo(saldo_novo)
            self._saldos.inserir(disp["id"], saldo_fmt)
            resposta = {
                "status": "aceita",
                "id_transacao": leitura.id_transacao,
                "alarme_servidor": alarme_depois,
                "media_geral": saldo_fmt["media_geral"],
                "total_leituras": saldo_fmt["total_leituras"],
            }
            if len(self._ids_processados) >= MAX_IDS_EM_CACHE:
                self._ids_processados.limpar()   # o UNIQUE do banco continua garantindo
            self._ids_processados.inserir(leitura.id_transacao, resposta)
            return resposta

    def _resposta_de_transacao_existente(self, id_transacao):
        con = self._conectar()
        try:
            tx = con.execute("SELECT * FROM transacoes WHERE id_transacao = ?",
                             (id_transacao,)).fetchone()
        finally:
            con.close()
        saldo = self._saldos.buscar(tx["dispositivo_id"])
        resposta = {
            "status": "duplicada",
            "id_transacao": id_transacao,
            "alarme_servidor": bool(tx["alarme_servidor"]),
            "media_geral": saldo["media_geral"],
            "total_leituras": saldo["total_leituras"],
        }
        self._ids_processados.inserir(id_transacao, {**resposta, "status": "aceita"})
        return resposta

    # ============================================================ consultas
    @staticmethod
    def _formatar_saldo(linha):
        s = dict(linha)
        total = s["total_leituras"]
        s["media_geral"] = round(s["soma_temperatura"] / total, 2) if total else 0.0
        s["soma_temperatura"] = round(s["soma_temperatura"], 2)
        s["alarme_ativo"] = bool(s["alarme_ativo"])
        return s

    def buscar_saldo(self, dispositivo_id):
        """Consulta O(1) direto da tabela hash, sem ir ao banco."""
        return self._saldos.buscar(dispositivo_id)

    def listar_transacoes(self, dispositivo_id=None, limite=20):
        con = self._conectar()
        try:
            if dispositivo_id:
                linhas = con.execute(
                    "SELECT * FROM transacoes WHERE dispositivo_id = ? ORDER BY id DESC LIMIT ?",
                    (dispositivo_id, limite)).fetchall()
            else:
                linhas = con.execute(
                    "SELECT * FROM transacoes ORDER BY id DESC LIMIT ?", (limite,)).fetchall()
            return [dict(l) for l in linhas]
        finally:
            con.close()

    def listar_auditoria(self, evento=None, limite=50):
        con = self._conectar()
        try:
            if evento:
                linhas = con.execute(
                    "SELECT * FROM logs_auditoria WHERE evento = ? ORDER BY id DESC LIMIT ?",
                    (evento, limite)).fetchall()
            else:
                linhas = con.execute(
                    "SELECT * FROM logs_auditoria ORDER BY id DESC LIMIT ?", (limite,)).fetchall()
            resultado = []
            for l in linhas:
                d = dict(l)
                d["detalhes"] = json.loads(d["detalhes"]) if d["detalhes"] else None
                resultado.append(d)
            return resultado
        finally:
            con.close()
