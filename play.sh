#!/bin/sh
# Avvia Adventureland (o un altro gioco: ./play.sh adv02)
# Opzionale secondo argomento: file di salvataggio da caricare.
DIR="$(cd "$(dirname "$0")" && pwd)"
GAME="${1:-adv01}"
exec "$DIR/src/ScottCurses" "$DIR/games/$GAME.dat" ${2:+"$2"}
