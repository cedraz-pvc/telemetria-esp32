from machine import Pin
import dht
import time
import sys
import select

LIMITE = 26.0        # a partir daqui o LED pisca como alarme (graus C)
HISTERESE = 0.5      # alarme desliga abaixo de LIMITE - HISTERESE
MEDIA_N = 3
INTERVALO_MS = 2000

sensor = dht.DHT11(Pin(4))
led = Pin(2, Pin.OUT)
led.off()

teclado = select.poll()
teclado.register(sys.stdin, select.POLLIN)


def bip(vezes=2, duracao_ms=150, pausa_ms=100):
    """'Bip' visual: pisca o LED. Quando tiver o buzzer, so trocar aqui."""
    for _ in range(vezes):
        led.on()
        time.sleep_ms(duracao_ms)
        led.off()
        time.sleep_ms(pausa_ms)


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


historico = []
alarme = False
ultima_leitura = time.ticks_ms() - INTERVALO_MS
rodando = True

print("Alarme (LED piscando) a partir de {:.1f} C".format(LIMITE))
print("Para parar: digite q e aperte Enter")
bip(1, 80)

while rodando:
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
                temp, media, umid, "PISCANDO" if alarme else "desligado"))

            if alarme:
                bip(2)

    if teclado.poll(100):
        if sys.stdin.read(1) in ("q", "Q"):
            rodando = False

led.off()
print("Parado. LED desligado.")