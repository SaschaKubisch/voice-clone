#!/bin/bash
# Double-click in Finder: starts the VoxCPM2 web interface and opens the browser.
cd "$(dirname "$0")" || exit 1
.venv/bin/python app.py
echo
read -r -p "The app has stopped. Press Return to close this window."
