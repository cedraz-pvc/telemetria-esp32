-- =====================================================================
-- schema.sql — Modelo relacional do backend de telemetria do ESP32
-- Banco: SQLite 3.35+ (notas para PostgreSQL no README)
--
-- Cada leitura enviada pelo ESP32 é tratada como uma TRANSAÇÃO:
--   * transacoes      -> registro imutável de cada leitura aceita
--   * saldos          -> estado consolidado por dispositivo (acumulados)
--   * logs_auditoria  -> trilha append-only de tudo que aconteceu,
--                        inclusive requisições rejeitadas
-- =====================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- Dispositivos autorizados a enviar dados
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dispositivos (
    id              TEXT    PRIMARY KEY,                 -- ex.: 'esp32-01'
    descricao       TEXT    NOT NULL,
    token_hash      TEXT    NOT NULL,                    -- SHA-256 do token (nunca o token puro)
    limite_alarme   REAL    NOT NULL DEFAULT 23.0,       -- °C
    histerese       REAL    NOT NULL DEFAULT 0.5,        -- °C
    ativo           INTEGER NOT NULL DEFAULT 1 CHECK (ativo IN (0, 1)),
    criado_em       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ---------------------------------------------------------------------
-- Transações: uma linha por leitura aceita (imutável)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transacoes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    id_transacao    TEXT    NOT NULL UNIQUE,             -- gerado no ESP32 -> garante idempotência
    dispositivo_id  TEXT    NOT NULL REFERENCES dispositivos (id),
    seq             INTEGER NOT NULL CHECK (seq >= 0),   -- contador de envio do ESP32
    temperatura     REAL    NOT NULL CHECK (temperatura BETWEEN 0 AND 50),
    umidade         REAL    NOT NULL CHECK (umidade BETWEEN 5 AND 95),
    media_movel     REAL    NOT NULL CHECK (media_movel BETWEEN 0 AND 50),
    alarme_esp32    INTEGER NOT NULL CHECK (alarme_esp32 IN (0, 1)),   -- estado informado pela placa
    alarme_servidor INTEGER NOT NULL CHECK (alarme_servidor IN (0, 1)), -- estado recalculado no backend
    recebido_em     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS ix_transacoes_disp_data
    ON transacoes (dispositivo_id, recebido_em DESC);

-- ---------------------------------------------------------------------
-- Saldos: estado consolidado por dispositivo (1:1 com dispositivos)
-- Atualizado na MESMA transação de banco que insere a leitura.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS saldos (
    dispositivo_id      TEXT    PRIMARY KEY REFERENCES dispositivos (id),
    total_leituras      INTEGER NOT NULL DEFAULT 0 CHECK (total_leituras >= 0),
    soma_temperatura    REAL    NOT NULL DEFAULT 0,
    temp_min            REAL,
    temp_max            REAL,
    ultima_temperatura  REAL,
    ultima_umidade      REAL,
    alarme_ativo        INTEGER NOT NULL DEFAULT 0 CHECK (alarme_ativo IN (0, 1)),
    total_alarmes       INTEGER NOT NULL DEFAULT 0 CHECK (total_alarmes >= 0),
    versao              INTEGER NOT NULL DEFAULT 0,      -- incrementa a cada atualização
    atualizado_em       TEXT
);

-- ---------------------------------------------------------------------
-- Logs de auditoria (append-only)
-- dispositivo_id SEM chave estrangeira de propósito: precisamos registrar
-- tentativas vindas de dispositivos desconhecidos.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS logs_auditoria (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ocorrido_em     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    evento          TEXT    NOT NULL CHECK (evento IN (
                        'DISPOSITIVO_CADASTRADO',
                        'TRANSACAO_ACEITA',
                        'TRANSACAO_DUPLICADA',
                        'REJEITADA_AUTENTICACAO',
                        'REJEITADA_VALIDACAO',
                        'ALARME_LIGADO',
                        'ALARME_DESLIGADO',
                        'DIVERGENCIA_ALARME',
                        'ERRO_INTERNO')),
    dispositivo_id  TEXT,
    id_transacao    TEXT,
    ip_origem       TEXT,
    detalhes        TEXT                                 -- JSON
);

CREATE INDEX IF NOT EXISTS ix_logs_evento   ON logs_auditoria (evento, ocorrido_em DESC);
CREATE INDEX IF NOT EXISTS ix_logs_disp     ON logs_auditoria (dispositivo_id, ocorrido_em DESC);

-- ---------------------------------------------------------------------
-- Imutabilidade: transações e logs não podem ser alterados nem apagados
-- ---------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS trg_transacoes_sem_update
BEFORE UPDATE ON transacoes
BEGIN
    SELECT RAISE(ABORT, 'transacoes sao imutaveis');
END;

CREATE TRIGGER IF NOT EXISTS trg_transacoes_sem_delete
BEFORE DELETE ON transacoes
BEGIN
    SELECT RAISE(ABORT, 'transacoes sao imutaveis');
END;

CREATE TRIGGER IF NOT EXISTS trg_logs_sem_update
BEFORE UPDATE ON logs_auditoria
BEGIN
    SELECT RAISE(ABORT, 'logs de auditoria sao append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_logs_sem_delete
BEFORE DELETE ON logs_auditoria
BEGIN
    SELECT RAISE(ABORT, 'logs de auditoria sao append-only');
END;
