FROM python:3.13-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir .

CMD ["sh", "-c", "set -a; . /etc/secrets/runtime.env; set +a; exec python live_trader.py"]
