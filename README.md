# BINANCE-ROBOT

Robot de trading Binance Spot (paper -> testnet -> réel), piloté par Telegram.
Aucun gain n'est garanti : le trading peut entraîner la perte totale du capital.

## Installation
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # puis éditez .env
pytest
```
