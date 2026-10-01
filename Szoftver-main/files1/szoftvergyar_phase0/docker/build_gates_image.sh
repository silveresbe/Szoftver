#!/bin/sh
# Megépíti a kapu-képet. A címkének egyeznie kell a factory.yaml `sandbox.image` értékével.
set -eu
TAG="${1:-fsz-gates:1}"
docker build -f "$(dirname "$0")/gates.Dockerfile" -t "$TAG" "$(dirname "$0")"
echo "kész: $TAG (állítsd be: sandbox.image: \"$TAG\"; az önteszt az első indításkor újra lefut)"
