#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT
javac -encoding UTF-8 -d "$OUT" \
  "$ROOT/app/src/main/java/com/finfam/companion/parser/ParsedNotification.java" \
  "$ROOT/app/src/main/java/com/finfam/companion/parser/NotificationParser.java" \
  "$ROOT/parser-tests/src/com/finfam/companion/parser/ParserSelfTest.java"
java -cp "$OUT" com.finfam.companion.parser.ParserSelfTest
