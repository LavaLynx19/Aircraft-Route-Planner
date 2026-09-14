#!/usr/bin/env bash
# Phase 4 end-to-end verification: success criteria + edge cases.
# Assumes the API server is running at http://localhost:${PORT:-8080}
# with OpenFlights data loaded into Neo4j.

set -u

BASE="${BASE:-http://localhost:${PORT:-8080}}"
PASS=0
FAIL=0

green() { printf '\033[32m%s\033[0m' "$1"; }
red()   { printf '\033[31m%s\033[0m' "$1"; }
bold()  { printf '\033[1m%s\033[0m' "$1"; }

pass() { PASS=$((PASS+1)); printf '  %s %s\n' "$(green PASS)" "$1"; }
fail() { FAIL=$((FAIL+1)); printf '  %s %s\n' "$(red FAIL)" "$1"; if [ -n "${2:-}" ]; then printf '       %s\n' "$2"; fi; }

section() { printf '\n%s\n' "$(bold "$1")"; }

# req URL → echoes the HTTP status code; response body is written to $BODY_FILE.
BODY_FILE=$(mktemp)
trap 'rm -f "$BODY_FILE"' EXIT
req() { curl -sS -o "$BODY_FILE" -w '%{http_code}' "$1" 2>/dev/null; }
get_body() { cat "$BODY_FILE"; }

# extract a numeric "count" field — works without jq via grep.
count_of() { printf '%s' "$1" | grep -oE '"count"[[:space:]]*:[[:space:]]*[0-9]+' | head -1 | grep -oE '[0-9]+'; }
contains() { printf '%s' "$1" | grep -q "$2"; }

section "1. Health"

status=$(req "$BASE/health"); body=$(get_body)
if [ "$status" = "200" ] && contains "$body" '"status":"ok"' && contains "$body" '"neo4j_ok":true'; then
  pass "/health → status=ok, neo4j_ok=true"
else
  fail "/health failed (status=$status)" "$body"
fi

section "2. Success criteria"

status=$(req "$BASE/routes?aircraft=388&from=LHR&to=SYD&max_hops=2"); body=$(get_body)
n=$(count_of "$body")
if [ "$status" = "200" ] && [ -n "$n" ] && [ "$n" -ge 1 ]; then
  pass "A380 LHR→SYD (max_hops=2) → $n route(s)"
else
  fail "A380 LHR→SYD returned no paths (status=$status, count=$n)" "$body"
fi

status=$(req "$BASE/routes/from?aircraft=388&from=LHR&max_hops=2"); body=$(get_body)
n=$(count_of "$body")
if [ "$status" = "200" ] && [ -n "$n" ] && [ "$n" -ge 20 ]; then
  pass "A380 from LHR (max_hops=2) → $n destinations (≥20)"
else
  fail "A380 from LHR returned <20 destinations (count=$n)" "$body"
fi

# Latency: 100 warm queries, compute median in ms.
section "3. Latency (LHR→SYD A380, 100 warm queries)"
curl -sS -o /dev/null "$BASE/routes?aircraft=388&from=LHR&to=SYD&max_hops=2"  # warmup
times_file=$(mktemp)
i=0
while [ $i -lt 100 ]; do
  curl -sS -o /dev/null -w '%{time_total}\n' \
    "$BASE/routes?aircraft=388&from=LHR&to=SYD&max_hops=2" >> "$times_file"
  i=$((i+1))
done
read -r p50 p95 <<EOF
$(awk '{print $1*1000}' "$times_file" | sort -n | awk '
  { a[NR]=$1 }
  END {
    n=NR;
    p50_idx = int(n*0.5);  if (p50_idx<1) p50_idx=1;
    p95_idx = int(n*0.95); if (p95_idx<1) p95_idx=1;
    printf "%.1f %.1f", a[p50_idx], a[p95_idx]
  }')
EOF
rm -f "$times_file"
if awk -v p="$p50" 'BEGIN{exit !(p<50)}'; then
  pass "p50=${p50}ms p95=${p95}ms (target: p50<50ms)"
else
  fail "p50=${p50}ms exceeds 50ms target (p95=${p95}ms)"
fi

section "4. Edge cases"

status=$(req "$BASE/routes?aircraft=XYZ&from=LHR&to=SYD"); body=$(get_body)
if [ "$status" = "404" ] && contains "$body" 'INVALID_AIRCRAFT'; then
  pass "Unknown aircraft → 404 INVALID_AIRCRAFT"
else
  fail "Unknown aircraft (status=$status)" "$body"
fi

status=$(req "$BASE/routes?aircraft=388&from=ZZZ&to=SYD"); body=$(get_body)
if [ "$status" = "404" ] && contains "$body" 'INVALID_AIRPORT'; then
  pass "Unknown airport → 404 INVALID_AIRPORT"
else
  fail "Unknown airport (status=$status)" "$body"
fi

status=$(req "$BASE/routes?from=LHR&to=SYD"); body=$(get_body)
if [ "$status" = "400" ] && contains "$body" 'MISSING_PARAM'; then
  pass "Missing aircraft → 400 MISSING_PARAM"
else
  fail "Missing aircraft (status=$status)" "$body"
fi

status=$(req "$BASE/routes?aircraft=388&from=LHR&to=SYD&mode=banana"); body=$(get_body)
if [ "$status" = "400" ] && contains "$body" 'INVALID_MODE'; then
  pass "Invalid mode → 400 INVALID_MODE"
else
  fail "Invalid mode (status=$status)" "$body"
fi

status=$(req "$BASE/routes?aircraft=388&from=LHR&to=SYD&sort=banana"); body=$(get_body)
if [ "$status" = "400" ] && contains "$body" 'INVALID_SORT'; then
  pass "Invalid sort → 400 INVALID_SORT"
else
  fail "Invalid sort (status=$status)" "$body"
fi

status=$(req "$BASE/routes?aircraft=388&from=LHR&to=SYD&max_hops=99"); body=$(get_body)
if [ "$status" = "200" ]; then
  pass "max_hops=99 silently clamped (200 OK)"
else
  fail "max_hops=99 returned $status" "$body"
fi

status=$(req "$BASE/routes?aircraft=388&from=LHR&to=DXB&max_hops=0"); body=$(get_body)
n=$(count_of "$body")
if [ "$status" = "200" ] && [ -n "$n" ] && [ "$n" -ge 1 ]; then
  pass "max_hops=0 returns direct LHR→DXB on A380 (count=$n)"
else
  fail "max_hops=0 LHR→DXB on 388 (status=$status, count=$n)" "$body"
fi

# SYD has no outgoing 388 edge to LHR direct → expect empty
status=$(req "$BASE/routes?aircraft=388&from=SYD&to=LHR&max_hops=0"); body=$(get_body)
n=$(count_of "$body")
if [ "$status" = "200" ] && [ "$n" = "0" ]; then
  pass "No matching direct routes → 200 with count=0"
else
  fail "Empty result handling (status=$status, count=$n)" "$body"
fi

section "5. Search endpoints"

status=$(req "$BASE/airports/search?q=london"); body=$(get_body)
n=$(count_of "$body")
if [ "$status" = "200" ] && [ -n "$n" ] && [ "$n" -ge 1 ]; then
  pass "/airports/search?q=london → $n result(s)"
else
  fail "/airports/search?q=london (status=$status, count=$n)" "$body"
fi

status=$(req "$BASE/aircraft/search?q=A380"); body=$(get_body)
n=$(count_of "$body")
if [ "$status" = "200" ] && [ -n "$n" ] && [ "$n" -ge 1 ]; then
  pass "/aircraft/search?q=A380 → $n result(s)"
else
  fail "/aircraft/search?q=A380 (status=$status, count=$n)" "$body"
fi

status=$(req "$BASE/countries/search?q=united"); body=$(get_body)
n=$(count_of "$body")
if [ "$status" = "200" ] && [ -n "$n" ] && [ "$n" -ge 3 ]; then
  pass "/countries/search?q=united → $n result(s) (≥3)"
else
  fail "/countries/search?q=united (status=$status, count=$n)" "$body"
fi

section "6. Memory footprint (informational)"

if command -v docker >/dev/null 2>&1; then
  mem=$(docker stats --no-stream --format '{{.MemUsage}}' aircraft-route-neo4j 2>/dev/null || true)
  if [ -n "$mem" ]; then
    printf '  Neo4j container memory: %s (target: combined Neo4j+Go < 4GB)\n' "$mem"
  else
    printf '  (container "aircraft-route-neo4j" not found — skip memory check)\n'
  fi
else
  printf '  (docker not available — skip memory check)\n'
fi

section "Summary"
printf '  %s passed, %s failed\n' "$(green "$PASS")" "$([ "$FAIL" -eq 0 ] && green "$FAIL" || red "$FAIL")"

[ "$FAIL" -eq 0 ]
