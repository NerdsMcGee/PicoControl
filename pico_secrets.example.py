# pico_secrets.example.py -- template for pico_secrets.py
#
# 1. Copy this file to pico_secrets.py (that name is git-ignored).
# 2. Fill in your WiFi details and a long random token.
#    e.g.  python -c "import secrets; print(secrets.token_urlsafe(24))"
# 3. Copy pico_secrets.py onto the Pico next to main.py.
# 4. Enter the same token in Pico Control's Token box.

WIFI_SSID     = "your-wifi-name"
WIFI_PASSWORD = "your-wifi-password"
AUTH_TOKEN    = "change-this-to-a-long-random-string"
