#!/usr/bin/env bash
# One-shot operator script — close orphan C call after 2026-05-12 silent close-fail.
# Background: /close C260522C00135000 was invoked 2026-05-12 03:30 ET while options
# market was closed. /close returned {status:closed,count:1} but NO SELL was sent
# to the broker (force_close verify path treated get_option_position=None as
# residual_qty=0). Broker still holds 11 contracts; DB row is closed_manual.
#
# Run this at or after 9:31 ET on 2026-05-13 (options market open).

set -u
K="BDmDgY_ICswPC9bJTGAnA7iW6VOL0NjhfLdzFJk_aiYUffoDlc4N9NOITbQZljYo"
B="https://stockrecs-yji2777elq-uc.a.run.app"
OCC="C260522C00135000"

echo "=== positions pre-close ==="
curl -sS -H "X-API-Key: $K" "$B/api/trading/positions" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); [print(p['symbol'],'qty=',p['qty'],'uPnL=',p['unrealized_pl']) for p in d]"

echo
echo "=== POST /close/$OCC ==="
curl -sS -X POST -H "X-API-Key: $K" "$B/api/trading/close/$OCC" | python3 -m json.tool

echo
echo "=== sleep 5s, then verify ==="
sleep 5
curl -sS -H "X-API-Key: $K" "$B/api/trading/positions" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); occ='C260522C00135000'; rem=[p for p in d if p['symbol']==occ]; print('REMAINING:', rem if rem else 'CLEAR')"

echo
echo "If REMAINING is still showing 11 contracts, fallback: submit direct SELL order"
echo "curl -X POST -H \"X-API-Key: \$K\" -H 'Content-Type: application/json' \\"
echo "  -d '{\"symbol\":\"$OCC\",\"qty\":11,\"side\":\"sell\",\"entry_type\":\"market\"}' \\"
echo "  $B/api/trading/order"
