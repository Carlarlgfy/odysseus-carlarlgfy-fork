#!/usr/bin/env python3
"""Native macOS window launcher for Odysseus Voice Test.
Starts the backend server then opens a WKWebView window — no browser required.
"""
import os
import sys
import signal
import subprocess
import time
import urllib.request
import urllib.error
import threading

INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get('ODYSSEUS_PORT', '7861'))
URL = f'http://127.0.0.1:{PORT}'
DATA_DIR = os.path.join(INSTALL_DIR, 'data-voice-test')

os.environ['ODYSSEUS_DATA_DIR'] = DATA_DIR
os.chdir(INSTALL_DIR)

LOG_DIR = os.path.join(INSTALL_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, 'odysseus-app.log')

_server_proc = None

def _wait_for_server(url, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False

def _start_server():
    global _server_proc
    uvicorn = os.path.join(INSTALL_DIR, 'venv', 'bin', 'uvicorn')
    log_file = open(LOG_PATH, 'a')
    _server_proc = subprocess.Popen(
        [uvicorn, 'app:app', '--host', '127.0.0.1', '--port', str(PORT)],
        cwd=INSTALL_DIR,
        stdout=log_file,
        stderr=log_file,
        env=os.environ.copy(),
    )
    return _server_proc

def _cleanup(sig=None, frame=None):
    if _server_proc and _server_proc.poll() is None:
        _server_proc.terminate()
        try:
            _server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server_proc.kill()
    sys.exit(0)

signal.signal(signal.SIGTERM, _cleanup)
signal.signal(signal.SIGINT, _cleanup)

# Check if already running
try:
    urllib.request.urlopen(URL, timeout=2)
    already_running = True
except Exception:
    already_running = False

if not already_running:
    _start_server()
    if not _wait_for_server(URL, timeout=90):
        import subprocess as sp
        sp.run([
            '/usr/bin/osascript', '-e',
            f'display dialog "Odysseus Voice Test failed to start.\\nSee: {LOG_PATH}" '
            'with title "Odysseus" buttons {"OK"} default button 1 with icon stop'
        ])
        _cleanup()

import webview

window = webview.create_window(
    'Odysseus Voice Test',
    URL,
    width=1400,
    height=900,
    min_size=(900, 600),
    background_color='#1a1a2e',
)

def _on_closed():
    _cleanup()

window.events.closed += _on_closed

webview.start(debug=False)
