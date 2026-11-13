# Copie este arquivo para config.py e preencha. O config.py NÃO vai para o GitHub
# (está no .gitignore), assim a senha do Wi-Fi não fica pública.
#
# No Thonny, salve na placa: config.py e main.py

WIFI_SSID = "NOME_DA_REDE"
WIFI_SENHA = "SENHA_DA_REDE"

# IP do computador que está rodando o backend (mesma rede Wi-Fi do ESP32).
# Windows: ipconfig  |  Linux/Mac: ip a
URL_API = "http://192.168.0.10:8000/api/leituras"

DISPOSITIVO_ID = "esp32-01"
TOKEN = "troque-este-token"     # o mesmo de TOKEN_DISPOSITIVO no servidor
