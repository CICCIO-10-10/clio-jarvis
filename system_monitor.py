"""
CLIO - system_monitor.py
Monitoraggio Proattivo del Sistema & Allarmi Vocali in Tempo Reale.

Controlla periodicamente (ogni 5 minuti):
1. Uso CPU (% > 90)
2. Uso RAM (% > 90)
3. Spazio libero su disco C: (< 10 GB)

In caso di anomalie, avvisa il Capo con voce naturale (edge-tts + pygame):
- "Capo, la RAM è quasi piena: stiamo usando il X%!"
- "Capo, la CPU è sotto sforzo eccessivo: utilizzo al X%!"
- "Capo, lo spazio sul disco C sta finendo: restano solo X GB!"
"""

import os
import sys
import time
import asyncio
import tempfile
import logging
from pathlib import Path
import psutil

# Fix encoding console Windows
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Configura logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [SystemMonitor] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("SystemMonitor")

# Parametri e Soglie di allarme
CPU_THRESHOLD_PERCENT = 90.0
RAM_THRESHOLD_PERCENT = 90.0
DISK_MIN_FREE_GB = 10.0
CHECK_INTERVAL_SECONDS = 300  # 5 minuti

# Cooldown per evitare ripetizioni continue dello stesso allarme (15 minuti)
ALERT_COOLDOWN_SECONDS = 900
_last_alerts = {
    "cpu": 0.0,
    "ram": 0.0,
    "disk": 0.0,
}

# FIX 1: voce unica centralizzata in config.TTS_VOICE (nessun fallback)
try:
    from config import TTS_VOICE as VOICE_NAME
except Exception:
    VOICE_NAME = "it-IT-IsabellaNeural"


async def _generate_speech(text: str, output_path: str):
    """Genera file audio MP3 con edge-tts."""
    import edge_tts
    comm = edge_tts.Communicate(text, VOICE_NAME, rate="+5%", pitch="+0Hz")
    await comm.save(output_path)


def speak_alert(text: str):
    """Pronuncia l'allarme a voce tramite edge-tts e pygame."""
    logger.info(f"🎙️ [VOCAL ALERT] Riproduzione allarme: '{text}'")
    tmp_path = None
    try:
        import pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init()

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tmp_path = f.name

        asyncio.run(_generate_speech(text, tmp_path))

        pygame.mixer.music.load(tmp_path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.1)
        pygame.mixer.music.unload()
    except Exception as e:
        logger.error(f"Errore riproduzione vocale edge-tts: {e}")
        # Fallback console bell
        print(f"\a[ALLARME CLIO]: {text}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def check_system_health(force_speak: bool = False) -> dict:
    """Esegue un check dei parametri di sistema e attiva gli avvisi vocali se necessario."""
    now = time.time()
    cpu_pct = psutil.cpu_percent(interval=1.0)
    ram = psutil.virtual_memory()
    ram_pct = ram.percent
    disk = psutil.disk_usage("C:\\")
    disk_free_gb = disk.free / (1024 ** 3)

    telemetry = {
        "cpu_percent": cpu_pct,
        "ram_percent": ram_pct,
        "ram_used_gb": ram.used / (1024 ** 3),
        "ram_total_gb": ram.total / (1024 ** 3),
        "disk_free_gb": disk_free_gb,
        "disk_total_gb": disk.total / (1024 ** 3),
        "alerts_triggered": []
    }

    logger.info(f"📊 Telemetria: CPU {cpu_pct:.1f}% | RAM {ram_pct:.1f}% | Disco C: libero {disk_free_gb:.1f} GB")

    # 1. Controllo RAM
    if ram_pct >= RAM_THRESHOLD_PERCENT:
        if force_speak or (now - _last_alerts["ram"] > ALERT_COOLDOWN_SECONDS):
            msg = f"Capo, la memoria RAM è quasi piena: stiamo usando il {int(ram_pct)} per cento!"
            telemetry["alerts_triggered"].append("RAM")
            _last_alerts["ram"] = now
            speak_alert(msg)

    # 2. Controllo CPU
    if cpu_pct >= CPU_THRESHOLD_PERCENT:
        if force_speak or (now - _last_alerts["cpu"] > ALERT_COOLDOWN_SECONDS):
            msg = f"Capo, il processore è sotto sforzo: utilizzo CPU al {int(cpu_pct)} per cento!"
            telemetry["alerts_triggered"].append("CPU")
            _last_alerts["cpu"] = now
            speak_alert(msg)

    # 3. Controllo Spazio Disco
    if disk_free_gb <= DISK_MIN_FREE_GB:
        if force_speak or (now - _last_alerts["disk"] > ALERT_COOLDOWN_SECONDS):
            msg = f"Capo, lo spazio su disco C sta finendo: restano solo {int(disk_free_gb)} giga liberi!"
            telemetry["alerts_triggered"].append("DISK")
            _last_alerts["disk"] = now
            speak_alert(msg)

    # Registra nel log di Clio se ci sono stati allarmi
    if telemetry["alerts_triggered"]:
        try:
            import jarvis_brain
            jarvis_brain.log_clio_action(
                "system_alert",
                {"allarmi": telemetry["alerts_triggered"], "telemetria": telemetry},
                True,
                "Allarme vocale emesso"
            )
        except Exception:
            pass

    return telemetry


def install_startup_shortcut():
    """Installa o aggiorna lo script di avvio nella cartella Startup di Windows."""
    try:
        startup_dir = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup")
        if not os.path.exists(startup_dir):
            os.makedirs(startup_dir, exist_ok=True)

        bat_content = f"""@echo off
set SCRIPT=C:\\Users\\User\\Desktop\\PROGETTI\\clio\\system_monitor.py
set PYTHONW=C:\\Users\\User\\AppData\\Local\\Programs\\Python\\Python312\\pythonw.exe
if not exist "%PYTHONW%" set PYTHONW=pythonw.exe
start "" "%PYTHONW%" "%SCRIPT%"
exit
"""
        target_bat = os.path.join(startup_dir, "avvia_system_monitor.bat")
        with open(target_bat, "w", encoding="utf-8") as f:
            f.write(bat_content)
        logger.info(f"✅ Startup batch installato in: {target_bat}")
        return True
    except Exception as e:
        logger.warning(f"Impossibile installare shortcut di avvio: {e}")
        return False


def run_daemon():
    """Esegue il loop di monitoraggio continuo ogni 5 minuti."""
    print("=" * 65)
    print("🛡️ CLIO - MONITORAGGIO PROATTIVO DEL SISTEMA")
    print(f"⏱️ Intervallo controllo: ogni {CHECK_INTERVAL_SECONDS // 60} minuti.")
    print(f"🚨 Soglie: CPU > {CPU_THRESHOLD_PERCENT}%, RAM > {RAM_THRESHOLD_PERCENT}%, Disco C: < {DISK_MIN_FREE_GB} GB")
    print("=" * 65)

    install_startup_shortcut()
    # Primo controllo all'avvio
    check_system_health()

    try:
        while True:
            time.sleep(CHECK_INTERVAL_SECONDS)
            check_system_health()
    except KeyboardInterrupt:
        print("\nArresto del System Monitor in corso...")


def main():
    if "--install-startup" in sys.argv:
        ok = install_startup_shortcut()
        print("Installazione avvio automatico:", "OK" if ok else "FALLITA")
        return

    if "--test-voice" in sys.argv:
        print("Test vocale in corso...")
        speak_alert("Capo, sistema di monitoraggio vocale di Clio attivo e perfettamente funzionante!")
        print("Test vocale completato.")
        return

    if "--check-now" in sys.argv:
        t = check_system_health()
        print("\n--- RISULTATO TELEMETRIA SISTEMA ---")
        print(f"CPU: {t['cpu_percent']:.1f}% (Soglia allarme: {CPU_THRESHOLD_PERCENT}%)")
        print(f"RAM: {t['ram_percent']:.1f}% ({t['ram_used_gb']:.1f} GB / {t['ram_total_gb']:.1f} GB) (Soglia allarme: {RAM_THRESHOLD_PERCENT}%)")
        print(f"Disco C: {t['disk_free_gb']:.1f} GB liberi su {t['disk_total_gb']:.1f} GB (Soglia allarme: {DISK_MIN_FREE_GB} GB)")
        if t["alerts_triggered"]:
            print(f"🚨 Allarmi attivati: {', '.join(t['alerts_triggered'])}")
        else:
            print("✅ Tutti i parametri sono entro i limiti di sicurezza.")
        return

    run_daemon()


if __name__ == "__main__":
    main()
