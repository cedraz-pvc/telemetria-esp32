"""
Simula o ESP32 enviando leituras para a API — útil para demonstrar sem a placa.

Uso (com o servidor rodando):
    python simulador_esp32.py                 # 15 leituras, temperatura sobe e desce
    python simulador_esp32.py --cenarios      # + reenvio duplicado, token errado e JSON inválido
"""
import argparse
import json
import os
import time
import urllib.error
import urllib.request

URL = os.getenv("URL_API", "http://localhost:8000/api/leituras")
TOKEN = os.getenv("TOKEN_DISPOSITIVO", "troque-este-token")
DISPOSITIVO = "esp32-01"
LIMITE = float(os.getenv("LIMITE_ALARME", "23.0"))
HISTERESE, MEDIA_N = 0.5, 3


def enviar(payload, token=TOKEN):
    req = urllib.request.Request(
        URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Device-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cenarios", action="store_true")
    ap.add_argument("--intervalo", type=float, default=0.5)
    args = ap.parse_args()

    boot = os.urandom(4).hex()
    curva = [21.0, 21.5, 22.1, 22.8, 23.4, 24.0, 24.3, 23.9, 23.2, 22.6, 22.2, 21.8, 21.5, 21.3, 21.1]
    historico, alarme, ultimo = [], False, None

    for seq, temp in enumerate(curva):
        historico = (historico + [temp])[-MEDIA_N:]
        media = sum(historico) / len(historico)
        if not alarme and media >= LIMITE:
            alarme = True
        elif alarme and media < LIMITE - HISTERESE:
            alarme = False
        ultimo = {
            "dispositivo_id": DISPOSITIVO,
            "id_transacao": "{}-{}-{:06d}".format(DISPOSITIVO, boot, seq),
            "seq": seq, "temperatura": temp, "umidade": 60.0,
            "media_movel": round(media, 2), "alarme": alarme,
        }
        status, corpo = enviar(ultimo)
        print("seq {:2d} | {:.1f} C | media {:.2f} | HTTP {} | {}".format(
            seq, temp, media, status, corpo))
        time.sleep(args.intervalo)

    if args.cenarios:
        print("\n--- Reenvio da última leitura (deve vir 'duplicada') ---")
        print(enviar(ultimo))
        print("\n--- Token errado (deve vir 401) ---")
        print(enviar({**ultimo, "id_transacao": ultimo["id_transacao"] + "x"}, token="errado"))
        print("\n--- Temperatura impossível para o DHT11 (deve vir 422) ---")
        print(enviar({**ultimo, "id_transacao": ultimo["id_transacao"] + "y", "temperatura": 99}))


if __name__ == "__main__":
    main()
