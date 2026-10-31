"""
logging_setup.py — STEP 4 hardening.
- Alza a WARNING i logger di rete rumorosi (httpx, httpcore) che altrimenti
  loggano l'URL completo delle richieste, token Telegram incluso.
- Aggiunge un filtro di redazione dei secret a tutti gli handler del root logger
  (difesa in profondita': se un secret finisce in un messaggio, viene mascherato).
Idempotente: chiamare harden_logging() piu' volte non duplica i filtri.
"""
import logging
import re

# Pattern dei secret piu' comuni nel progetto. Ordine: dal piu' specifico al generico.
_PATTERNS = [
    # Token Telegram dentro URL: .../bot<token>/metodo
    (re.compile(r'bot\d{6,}:[A-Za-z0-9_-]{30,}'), 'bot<REDACTED_TELEGRAM_TOKEN>'),
    # Token Telegram "nudo": 8-12 cifre : 35 char
    (re.compile(r'\b\d{8,12}:[A-Za-z0-9_-]{35}\b'), '<REDACTED_TELEGRAM_TOKEN>'),
    # Google / Gemini API key
    (re.compile(r'AIza[0-9A-Za-z_-]{35}'), '<REDACTED_GOOGLE_KEY>'),
    # Groq
    (re.compile(r'gsk_[A-Za-z0-9]{20,}'), '<REDACTED_GROQ_KEY>'),
    # ElevenLabs / generico sk_
    (re.compile(r'sk_[A-Za-z0-9]{20,}'), '<REDACTED_SK_KEY>'),
    # query param ?key=... (es. endpoint Gemini REST)
    (re.compile(r'([?&]key=)[A-Za-z0-9_-]+'), r'\1<REDACTED>'),
    # Chiave interna Clio
    (re.compile(r'Cl10_[A-Za-z0-9_]+'), '<REDACTED_CLIO_KEY>'),
]

_ATTR = "_clio_redacted"


def _register_secret(value):
    """Aggiunge dinamicamente un pattern per il valore reale di un segreto."""
    if value and len(value) >= 8:
        try:
            _PATTERNS.insert(0, (re.compile(re.escape(value)), '<REDACTED_CLIO_KEY>'))
        except re.error:
            pass


def _redact(text):
    if not text:
        return text
    for rx, sub in _PATTERNS:
        text = rx.sub(sub, text)
    return text


class RedactionFilter(logging.Filter):
    """Maschera i secret nel messaggio gia' formattato del record di log."""
    def filter(self, record):
        try:
            msg = record.getMessage()
            red = _redact(msg)
            if red != msg:
                record.msg = red
                record.args = ()
        except Exception:
            # Un filtro non deve mai rompere il logging.
            pass
        return True


_DONE = False


def harden_logging(extra_loggers=("httpx", "httpcore")):
    """Applica redazione + innalzamento livelli. Sicuro da chiamare piu' volte."""
    global _DONE
    for name in extra_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)

    root = logging.getLogger()
    # Aggancia il filtro a tutti gli handler del root (i log propagati da httpx & co.
    # passano da qui) evitando duplicati.
    for h in root.handlers:
        if not getattr(h, _ATTR, False):
            h.addFilter(RedactionFilter())
            setattr(h, _ATTR, True)

    # Dopo la rotazione la chiave Clio non ha più il prefisso "Cl10_": la redigiamo
    # registrando il valore reale letto da config (se presente).
    if not _DONE:
        try:
            import config as _cfg
            _register_secret(getattr(_cfg, "CLIO_API_KEY", None))
        except Exception:
            pass

    _DONE = True
    return _DONE
