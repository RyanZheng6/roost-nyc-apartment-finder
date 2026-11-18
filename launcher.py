"""Double-click launcher for Roost's local-only web interface."""
import multiprocessing
import os
import socket
import threading
import time
import webbrowser


def open_browser():
    # Open only once Uvicorn has bound the local port.
    for _ in range(80):
        try:
            with socket.create_connection(('127.0.0.1', 8000), timeout=0.25):
                webbrowser.open('http://127.0.0.1:8000')
                return
        except OSError:
            time.sleep(0.25)


def main():
    import uvicorn
    from server import app
    threading.Thread(target=open_browser, daemon=True).start()
    # PyInstaller --windowed sets sys.stdout/sys.stderr to None on Windows.
    # Uvicorn's default formatter probes stderr.isatty() and crashes on startup.
    # Disable console logging in the no-console executable.
    uvicorn.run(app, host='127.0.0.1', port=8000, reload=False,
                access_log=False, log_config=None)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
