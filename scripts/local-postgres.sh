#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DATA="$ROOT/.pgdata"
SOCKET="$ROOT/.pgsocket"
PORT=55432
case "${1:-}" in
  init)
    if [ -e "$DATA/PG_VERSION" ]; then echo 'Cluster local já existe.'; exit 0; fi
    mkdir -p "$SOCKET"
    chmod 700 "$SOCKET"
    initdb -D "$DATA" --auth-local=trust --auth-host=scram-sha-256 --no-instructions
    ;;
  start)
    [ -e "$DATA/PG_VERSION" ] || { echo 'Execute primeiro: scripts/local-postgres.sh init'; exit 1; }
    mkdir -p "$SOCKET"
    chmod 700 "$SOCKET"
    pg_ctl -D "$DATA" -l "$DATA/server.log" -o "-p $PORT -k $SOCKET -c listen_addresses=''" start
    if [ "$(PGHOST="$SOCKET" PGPORT="$PORT" psql -d postgres -Atqc "SELECT 1 FROM pg_database WHERE datname='finances'")" != 1 ]; then
      PGHOST="$SOCKET" PGPORT="$PORT" createdb finances
    fi
    echo "Banco pronto. Inicie a API com: PGHOST='$SOCKET' PGPORT=$PORT .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 3000"
    ;;
  stop) pg_ctl -D "$DATA" stop ;;
  status) pg_ctl -D "$DATA" status ;;
  *) echo 'Uso: scripts/local-postgres.sh init|start|stop|status'; exit 2 ;;
esac
