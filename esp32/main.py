"""
ESP32 + DHT11 — alarme de temperatura (LED + buzzer) com envio das leituras
em JSON para o backend (POST /api/leituras).

Versão 3.0 — base: versoes_anteriores/sensor_temperatura_com_buzzer_v2.py
  * buzzer passivo no GPIO18 via PWM + LED da placa (GPIO2)
  * machine.Timer faz a piscada/bip: o alarme não para enquanto o código
    espera o sensor ou a resposta do servidor
  * Wi-Fi + envio HTTP com token no cabeçalho X-Device-Token
  * id_transacao único por leitura -> reenvio seguro (o servidor é idempotente)
  * fila local: se a rede/servidor cair, guarda até FILA_MAX leituras e reenvia

Configuração de rede/token: copie config.exemplo.py para config.py e preencha.
"""
from machine import Pin, PWM, Timer
import dht
import time
import sys
import select
import os
import network
import ubinascii
import urequests

import config

LIMITE = 23.0         # °C que dispara o alarme (igual ao limite cadastrado no servidor)
HISTERESE = 0.5       # alarme só desliga abaixo de LIMITE - HISTERESE
MEDIA_N = 3           # leituras na média móvel
INTERVALO_MS = 2000   # intervalo entre leituras (DHT11: mínimo ~1 s)
VELOCIDADE_MS = 250   # velocidade da piscada e do bip
FREQ_BUZZER = 2048    # frequência do buzzer passivo (Hz)
DUTY_SOM = 512        # 50 % -> volume máximo do PWM
FILA_MAX = 20         # leituras guardadas enquanto o servidor estiver fora
TIMEOUT_HTTP_S = 3

sensor = dht.DHT11(Pin(4))
led = Pin(2, Pin.OUT)
buzzer = PWM(Pin(18), freq=FREQ_BUZZER, duty=0)
temporizador = Timer(0)

teclado = select.poll()
teclado.register(sys.stdin, select.POLLIN)

# Identificador desta "ligada" da placa: evita repetir id_transacao após reset
BOOT_ID = ubinascii.hexlify(os.urandom(4)).decode()
wlan = network.WLAN(network.STA_IF)

alarme = False
_estado_piscada = False


# ---------------------------------------------------------------- alarme
def piscar(_t):
    """Chamada pelo Timer a cada VELOCIDADE_MS: LED e buzzer juntos."""
    global _estado_piscada
    if alarme:
        _estado_piscada = not _estado_piscada
        led.value(_estado_piscada)
        buzzer.duty(DUTY_SOM if _estado_piscada else 0)
    elif _estado_piscada:
        _estado_piscada = False
        led.off()
        buzzer.duty(0)


def bip_curto(ms=120):
    """Bip de confirmação na partida."""
    led.on()
    buzzer.duty(DUTY_SOM)
    time.sleep_ms(ms)
    buzzer.duty(0)
    led.off()


def desligar_tudo():
    temporizador.deinit()
    buzzer.duty(0)
    buzzer.deinit()
    led.off()


# ---------------------------------------------------------------- sensor
def ler_dht11(tentativas=3):
    for _ in range(tentativas):
        try:
            sensor.measure()
            b = sensor.buf
            temp = b[2] + (b[3] & 0x7F) / 10
            if b[3] & 0x80:
                temp = -temp
            umid = b[0] + b[1] / 10
            if 0 <= temp <= 50 and 5 <= umid <= 95:
                return temp, umid
        except OSError:
            pass
        time.sleep_ms(1100)
    return None, None


# ------------------------------------------------------------------ rede
def conectar_wifi(espera_s=10):
    wlan.active(True)
    if wlan.isconnected():
        return True
    print("Conectando ao Wi-Fi '{}'...".format(config.WIFI_SSID))
    try:
        wlan.connect(config.WIFI_SSID, config.WIFI_SENHA)
    except OSError:
        pass
    inicio = time.ticks_ms()
    while not wlan.isconnected() and time.ticks_diff(time.ticks_ms(), inicio) < espera_s * 1000:
        time.sleep_ms(200)
    if wlan.isconnected():
        print("Wi-Fi OK. IP:", wlan.ifconfig()[0])
        return True
    print("Sem Wi-Fi por enquanto. As leituras ficam na fila.")
    return False


def post_json(payload):
    """Envia o JSON. Retorna (status_http, corpo) ou (None, erro)."""
    cabecalhos = {"Content-Type": "application/json", "X-Device-Token": config.TOKEN}
    try:
        try:
            r = urequests.post(config.URL_API, json=payload, headers=cabecalhos,
                               timeout=TIMEOUT_HTTP_S)
        except TypeError:   # versões antigas do urequests não aceitam timeout
            r = urequests.post(config.URL_API, json=payload, headers=cabecalhos)
        try:
            status = r.status_code
            corpo = r.json() if status in (200, 401, 422) else r.text
        finally:
            r.close()
        return status, corpo
    except Exception as erro:
        return None, erro


fila = []


def enfileirar(payload):
    fila.append(payload)
    if len(fila) > FILA_MAX:
        descartada = fila.pop(0)
        print("Fila cheia: descartada leitura seq", descartada["seq"])


def esvaziar_fila():
    """Envia em ordem. Para no primeiro erro de rede (tenta de novo no próximo ciclo)."""
    if not wlan.isconnected():
        conectar_wifi(espera_s=0)
        return
    while fila:
        status, corpo = post_json(fila[0])
        if status == 200:
            fila.pop(0)
            print("  -> servidor: {} | alarme servidor: {} | media geral: {:.1f} C".format(
                corpo["status"], corpo["alarme_servidor"], corpo["media_geral"]))
        elif status in (401, 422):
            fila.pop(0)     # reenviar não resolve: descarta e avisa
            print("  -> REJEITADA ({}): {}".format(status, corpo))
        else:
            print("  -> servidor indisponivel ({}). {} na fila.".format(
                status if status else corpo, len(fila)))
            return


# ----------------------------------------------------------------- laço
historico = []
seq = 0
ultima_leitura = time.ticks_ms() - INTERVALO_MS

try:
    conectar_wifi()
    temporizador.init(period=VELOCIDADE_MS, mode=Timer.PERIODIC, callback=piscar)
    print("Alarme (LED + buzzer) a partir de {:.1f} C".format(LIMITE))
    print("Enviando para", config.URL_API)
    print("Para parar: digite q e aperte Enter (ou Ctrl+C)")
    bip_curto()

    while True:
        if time.ticks_diff(time.ticks_ms(), ultima_leitura) >= INTERVALO_MS:
            ultima_leitura = time.ticks_ms()
            temp, umid = ler_dht11()

            if temp is None:
                print("Falha na leitura (3 tentativas). Confira a ligacao.")
            else:
                historico.append(temp)
                if len(historico) > MEDIA_N:
                    historico.pop(0)
                media = sum(historico) / len(historico)

                if not alarme and media >= LIMITE:
                    alarme = True
                    print(">>> ALERTA: temperatura chegou em {:.1f} C".format(media))
                elif alarme and media < LIMITE - HISTERESE:
                    alarme = False
                    print(">>> Temperatura normalizada")

                print("Atual: {:.1f} C | Media: {:.1f} C | Umidade: {:.0f} % | Alarme: {}".format(
                    temp, media, umid, "LIGADO" if alarme else "desligado"))

                enfileirar({
                    "dispositivo_id": config.DISPOSITIVO_ID,
                    "id_transacao": "{}-{}-{:06d}".format(config.DISPOSITIVO_ID, BOOT_ID, seq),
                    "seq": seq,
                    "temperatura": round(temp, 1),
                    "umidade": round(umid, 1),
                    "media_movel": round(media, 2),
                    "alarme": alarme,
                })
                seq += 1

            esvaziar_fila()

        if teclado.poll(100):
            if sys.stdin.read(1) in ("q", "Q"):
                break
except KeyboardInterrupt:
    pass
finally:
    desligar_tudo()
    print("Parado. LED e buzzer desligados. {} leitura(s) nao enviada(s).".format(len(fila)))
