"""
Desktop launcher — starts Flask in a background thread then opens a
PySide6 native window with a Chromium-based webview (QtWebEngine).
Double-click this file or run via launch.bat to open the app with no terminal.
"""
import sys
import threading
import time
import urllib.request


def _start_flask():
    from app import app
    app.run(host='0.0.0.0', port=5050, debug=False, use_reloader=False, threaded=True)


def _wait_for_server(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.15)
    return False


if __name__ == "__main__":
    # Validate dependencies before touching Qt
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except ImportError:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk(); root.withdraw()
        messagebox.showerror(
            "Missing dependency",
            "PySide6 is not installed.\n\n"
            "Run:  pip install PySide6 flask\n"
            "then try again."
        )
        sys.exit(1)

    # Start Flask server in a daemon thread
    flask_thread = threading.Thread(target=_start_flask, daemon=True)
    flask_thread.start()

    url = "http://localhost:5050"
    if not _wait_for_server(url):
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk(); root.withdraw()
        messagebox.showerror("Startup error", "Flask server failed to start within 30 s.")
        sys.exit(1)

    from PySide6.QtWidgets import QApplication, QMainWindow
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtCore import QUrl, Qt

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("Sports Betting Analysis")
    qt_app.setApplicationDisplayName("Sports Betting Analysis")

    window = QMainWindow()
    window.setWindowTitle("Sports Betting Analysis")
    window.resize(1400, 920)
    window.setMinimumSize(900, 640)

    view = QWebEngineView()
    # Allow JS and local storage
    settings = view.settings()
    settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)

    view.load(QUrl(url))
    window.setCentralWidget(view)
    window.show()

    sys.exit(qt_app.exec())
