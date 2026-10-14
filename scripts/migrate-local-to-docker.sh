#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

if [ ! -S "$ROOT/.pgsocket/.s.PGSQL.55432" ]; then
  echo 'Banco local antigo não encontrado em .pgsocket:55432.' >&2
  exit 1
fi

EXISTING=$(docker compose exec -T db psql -U finances -d finances -Atqc 'SELECT count(*) FROM users')
if [ "$EXISTING" != 0 ]; then
  echo 'O banco do Docker já tem usuários. Migração cancelada para preservar os dados.' >&2
  exit 1
fi

DUMP=$(mktemp)
trap 'rm -f "$DUMP"' EXIT HUP INT TERM

set -- --table=public.users --table=public.sessions --table=public.accounts \
  --table=public.bills --table=public.transactions --table=public.investments
HAS_CATEGORIES=$(PGHOST="$ROOT/.pgsocket" PGPORT=55432 psql -d finances -Atqc "SELECT to_regclass('public.categories') IS NOT NULL")
if [ "$HAS_CATEGORIES" = t ]; then
  set -- "$@" --table=public.categories --table=public.subcategories
fi

PGHOST="$ROOT/.pgsocket" PGPORT=55432 pg_dump -d finances --data-only --column-inserts "$@" -f "$DUMP"
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U finances -d finances -c \
  'TRUNCATE TABLE sessions,transactions,bills,investments,accounts,subcategories,categories,users RESTART IDENTITY CASCADE'
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U finances -d finances < "$DUMP"

docker compose restart api
echo 'Dados importados. A API foi reiniciada para vincular as categorias existentes.'
