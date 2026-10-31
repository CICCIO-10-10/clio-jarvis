"""
Test di latenza e verifica ottimizzazioni per Clio.
Misura il tempo di risposta per 'Ciao, chi sei?' e verifica:
- Profilo e modello attivo
- Streaming delle risposte
- Cache risposte identiche
- Limiti massimi di caratteri del contesto Obsidian
"""
import sys
import time
import os

# Imposta il percorso del progetto Clio
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def run_latency_test():
    # Import pesanti SOLO all'esecuzione (clio_core avvia thread e tocca l'audio):
    # così pytest può importare il modulo senza effetti collaterali.
    import config
    import model_router
    from jarvis_brain import jarvis, _RESPONSE_CACHE
    import clio_core

    print("=" * 60)
    print("🚀 TEST LATENZA & VERIFICA OTTIMIZZAZIONI CLIO")
    print("=" * 60)

    # 1. Configurazione modelli e contesto
    profile_fast = model_router.get_profile("FAST")
    profile_general = model_router.get_profile("GENERAL")
    chain_fast = model_router.get_provider_chain("FAST")

    print(f"📌 Modello Primario Configurato: {config.MODEL_PRIMARY}")
    print(f"📌 Modello Groq Veloce (FAST): {profile_fast.get('provider')}:{profile_fast.get('model')}")
    print(f"📌 Modello Ollama Configurato: {config.OLLAMA_MODEL}")
    print(f"📌 Timeout LLM: {getattr(config, 'LLM_TIMEOUT', 30)}s")
    print(f"📌 Max Caratteri Contesto Obsidian (config): {getattr(config, 'MAX_CONTEXT_CHARS', 8000)}")
    print(f"📌 Max Caratteri Contesto Vault (clio_core): {getattr(clio_core, 'VAULT_MAX_CONTEXT_CHARS', 8000)}")
    print(f"📌 Catena Provider FAST: {chain_fast}")
    print("-" * 60)

    # 2. Test Latenza con Prompt 'Ciao, chi sei?' (Prima Chiamata - generazione fresca)
    prompt = "Ciao, chi sei?"
    print(f"\n[Test 1] Invio prompt al cervello di Clio: '{prompt}' (Cold / No Cache)...")

    streamed_chunks = []
    t_start = time.time()
    t_first = None

    def on_token(chunk):
        nonlocal t_first
        if t_first is None and chunk.strip():
            t_first = time.time() - t_start
        streamed_chunks.append(chunk)

    latency_metrics = {}
    risposta = jarvis.think_and_act(prompt, on_text=on_token, latency=latency_metrics)
    t_total = time.time() - t_start

    print(f"   ⏱️ Tempo al primo token (TTFT): {t_first*1000:.1f} ms" if t_first else "   ⏱️ Primo token: non registrato")
    print(f"   ⏱️ Tempo totale di risposta: {t_total:.3f} s")
    print(f"   📝 Risposta ricevuta ({len(risposta)} caratteri):")
    print(f"   \"{risposta}\"")
    print(f"   📡 Chunk ricevuti in streaming: {len(streamed_chunks)}")

    # 3. Test Latenza (Seconda Chiamata - Risposta da Cache)
    print(f"\n[Test 2] Invio prompt identico: '{prompt}' (Test Cache Risposte Identiche)...")
    t_start_cache = time.time()
    risposta_cache = jarvis.think_and_act(prompt)
    t_total_cache = time.time() - t_start_cache

    print(f"   ⚡ Tempo totale di risposta da Cache: {t_total_cache*1000:.2f} ms")
    print(f"   📝 Risposta da cache identica: {risposta_cache == risposta}")
    print(f"   📦 Voci totali in cache risposte: {len(_RESPONSE_CACHE)}")

    # 4. Verifica Limiti Contesto Obsidian
    print("\n[Test 3] Verifica generazione contesto Obsidian per query generica...")
    ctx = clio_core.get_vault_context_for_prompt("quali sono i miei progetti e abitudini?")
    print(f"   📊 Caratteri effettivi contesto generati: {len(ctx)} (limite max: {clio_core.VAULT_MAX_CONTEXT_CHARS})")
    assert len(ctx) <= clio_core.VAULT_MAX_CONTEXT_CHARS, "Il contesto supera il limite massimo consentito!"
    print("   ✅ Verifica limite caratteri contesto Obsidian: SUPERATA")

    # 5. Verifica Cache File Vault
    print("\n[Test 4] Verifica cache file letti del Vault...")
    print(f"   📂 File attualmente in cache lettura: {len(clio_core._VAULT_FILE_CACHE)}")

    print("\n" + "=" * 60)
    print("🎯 RIEPILOGO TEST LATENZA")
    print("=" * 60)
    print(f"• Modello in uso (FAST/Chat): {profile_fast.get('model')} ({profile_fast.get('provider')})")
    print(f"• Modello Fallback / Cloud:   {config.MODEL_PRIMARY}")
    print(f"• Modello Locale (Ollama):    {config.OLLAMA_MODEL}")
    print(f"• Max Contesto Obsidian:      {clio_core.VAULT_MAX_CONTEXT_CHARS} caratteri")
    print(f"• Timeout Richieste:          {getattr(config, 'LLM_TIMEOUT', 30)}s")
    print(f"• Latenza Cold (TTFT):        {t_first*1000:.1f} ms" if t_first else "N/A")
    print(f"• Latenza Totale (Cold):      {t_total:.3f} s")
    print(f"• Latenza da Cache:           {t_total_cache*1000:.2f} ms")
    print("=" * 60)

if __name__ == "__main__":
    run_latency_test()
