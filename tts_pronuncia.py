"""tts_pronuncia.py — correzioni di pronuncia per la voce di CLIO (edge-tts).

edge-tts legge alcuni nomi inglesi con le regole dell'italiano: "Paint" esce
come "Pa-ìnt" (e "mspaint" come "emme esse paint" storpiato). Qui c'è la mappa
delle sostituzioni: si applica al TESTO che va in sintesi, mai a quello mostrato
nell'interfaccia.

Il modulo è volutamente puro (solo `re`): `clio_core` lo importa al posto di
tenere la mappa in mezzo al runtime (che avvia thread e client all'import),
così la correzione è testabile in isolamento.

Uso:
    from tts_pronuncia import pronuncia_tts
    edge_tts.Communicate(pronuncia_tts(testo), ...)
"""
from __future__ import annotations

import re

# Chiave = come scrive CLIO, valore = come deve essere letto.
# Le chiavi più lunghe vincono ("mspaint" prima di "Paint").
PRONUNCIA_TTS = {
    "Paint": "Peint",
    "mspaint": "emme esse paint",
}

_PAROLE = sorted(PRONUNCIA_TTS, key=len, reverse=True)
_MAPPA_MINUSCOLA = {parola.lower(): lettura for parola, lettura in PRONUNCIA_TTS.items()}
# Confini di parola: "Paint" sì, "repaint" no; "mspaint.exe" sì, "mspaint2" no.
_PATTERN = re.compile(
    "|".join(rf"(?<!\w){re.escape(parola)}(?!\w)" for parola in _PAROLE),
    re.IGNORECASE,
)


def pronuncia_tts(testo):
    """Testo pronto per edge-tts con i nomi corretti. Non modifica l'input.

    Una sola passata: una sostituzione non viene riletta dalla mappa
    ("mspaint" -> "emme esse paint", non "emme esse Peint").
    """
    if not isinstance(testo, str) or not testo:
        return testo
    return _PATTERN.sub(
        lambda match: _MAPPA_MINUSCOLA.get(match.group(0).lower(), match.group(0)),
        testo,
    )
