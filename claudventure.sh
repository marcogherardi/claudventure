#!/bin/sh
# Adventureland (o un altro gioco) con Claude come narratore.
#   ./claudventure.sh                 Adventureland
#   ./claudventure.sh adv02           un altro gioco (./claudventure.sh --list per l'elenco)
#   ./claudventure.sh --load autosave riprende l'ultima partita
#   ./claudventure.sh --model haiku   usa Haiku invece di Sonnet
#   ./claudventure.sh --plain         testo a scorrimento invece dei pannelli
DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$DIR/claudventure/app.py" "$@"
