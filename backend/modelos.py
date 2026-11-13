"""Contratos de entrada/saída da API (validação feita pelo Pydantic)."""
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class LeituraEntrada(BaseModel):
    """JSON que o ESP32 envia a cada leitura do DHT11."""

    # Campos extras são recusados: o ESP32 só pode mandar o que está aqui.
    model_config = ConfigDict(extra="forbid")

    dispositivo_id: str = Field(pattern=r"^[A-Za-z0-9_-]{3,40}$")
    id_transacao: str = Field(pattern=r"^[A-Za-z0-9_-]{8,80}$")
    seq: int = Field(ge=0)
    temperatura: float = Field(ge=0, le=50)      # faixa de medição do DHT11
    umidade: float = Field(ge=5, le=95)          # faixa de medição do DHT11
    media_movel: float = Field(ge=0, le=50)
    alarme: StrictBool

    @model_validator(mode="after")
    def id_pertence_ao_dispositivo(self):
        # O id da transação precisa começar com o id do dispositivo,
        # evitando que uma placa "reaproveite" ids de outra.
        if not self.id_transacao.startswith(self.dispositivo_id + "-"):
            raise ValueError("id_transacao deve começar com '<dispositivo_id>-'")
        return self


class LeituraResposta(BaseModel):
    status: str                    # "aceita" | "duplicada"
    id_transacao: str
    alarme_servidor: bool
    media_geral: float
    total_leituras: int
