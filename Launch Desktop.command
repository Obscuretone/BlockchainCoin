#!/bin/sh
# Launch the native Python desktop console from this checkout.
cd "$(dirname "$0")" || exit 1
if [ -n "${BCC_DESKTOP_PYTHON:-}" ]; then
    desktop_python="$BCC_DESKTOP_PYTHON"
elif [ -x .desktop-venv/bin/python ]; then
    desktop_python=.desktop-venv/bin/python
elif [ -x .venv/bin/python ]; then
    desktop_python=.venv/bin/python
else
    desktop_python=python3
fi
if ! "$desktop_python" -c 'import tkinter, cryptography' >/dev/null 2>&1; then
    echo "Choose a Python installation with Tkinter and install BlockchainCoin there."
    echo "Set BCC_DESKTOP_PYTHON to that interpreter, then run this launcher again."
    exit 1
fi
exec "$desktop_python" -m blockchaincoin.gui "$@"
