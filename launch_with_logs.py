"""Menu entry: reuse Clio if running, and always show a live log console."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import webbrowser

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "AUTOMAZIONI"))
import hq_theme as UI

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"


def running():
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", 5000)) == 0


def current_log():
    candidates = [*LOGS.glob("menu-live-*.log"), *LOGS.glob("proactive-*.err.log"),
                  LOGS / "clio_run_err.log", LOGS / "clio_stderr.log"]
    files = [p for p in candidates if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime_ns) if files else None


def main():
    if "--check" in sys.argv:
        assert (ROOT / "run_clio.py").is_file()
        print("Launcher valido; nessun processo avviato.")
        return
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("Clio - log in tempo reale")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    UI.header("C.L.I.O.", "Console dei log in tempo reale", "Chiudere questa finestra non arresta Clio.")
    LOGS.mkdir(exist_ok=True)
    if not running():
        path = LOGS / ("menu-live-" + time.strftime("%Y%m%d-%H%M%S") + ".log")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        with path.open("ab") as output:
            subprocess.Popen([sys.executable, "-u", "-B", "run_clio.py", "--no-browser"],
                             cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        UI.message("Avvio Clio...", "step")
    else:
        UI.message("Clio già attivo: collegamento ai log.", "ok")
    opened = False
    handle = None
    selected = None
    try:
        while True:
            if not opened and running():
                webbrowser.open("http://localhost:5000/")
                opened = True
            latest = current_log()
            if latest and latest != selected:
                if handle:
                    handle.close()
                selected = latest
                handle = latest.open("r", encoding="utf-8", errors="replace")
                # Show recent context once; subsequent reads follow appended lines.
                UI.message("Log: " + str(latest), "info")
                print("".join(handle.readlines()[-35:]), end="", flush=True)
            elif handle:
                if selected.stat().st_size < handle.tell():
                    handle.seek(0)
                chunk = handle.read()
                if chunk:
                    print(chunk, end="", flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nVista log chiusa. Clio resta attivo.")
    finally:
        if handle:
            handle.close()


if __name__ == "__main__":
    main()
