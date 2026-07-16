#!/usr/bin/env bash

set -e

if command -v gnome-terminal >/dev/null 2>&1; then
    TERM_CMD="gnome-terminal -- bash -c"
elif command -v konsole >/dev/null 2>&1; then
    TERM_CMD="konsole -e bash -c"
elif command -v xfce4-terminal >/dev/null 2>&1; then
    TERM_CMD="xfce4-terminal -e"
elif command -v xterm >/dev/null 2>&1; then
    TERM_CMD="xterm -e"
else
    echo "No supported terminal emulator found."
    exit 1
fi

launch() {
    if [[ "$TERM_CMD" == "gnome-terminal -- bash -c" ]]; then
        gnome-terminal -- bash -c "$1; exec bash"
    elif [[ "$TERM_CMD" == "konsole -e bash -c" ]]; then
        konsole -e bash -c "$1; exec bash"
    elif [[ "$TERM_CMD" == "xfce4-terminal -e" ]]; then
        xfce4-terminal -e "bash -c '$1; exec bash'"
    else
        xterm -e "bash -c '$1; exec bash'" &
    fi
}

launch "npm run tailwind"
launch "cd src && python manage.py runserver"
launch "cd src && celery -A config worker --loglevel DEBUG --pool=solo"
launch "cd src && celery -A config beat --scheduler django --loglevel DEBUG"

echo "All services started ready for tests at http://127.0.0.1:8000/."