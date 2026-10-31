import cost_guard
import model_router


def test_coder_locale_senza_fallback_cloud_e_gratuito(monkeypatch):
    monkeypatch.delenv("CLIO_CODING_AGENT", raising=False)
    monkeypatch.setattr(model_router, "get_profile", lambda _: {"provider": "ollama"})
    monkeypatch.setattr(model_router, "get_provider_chain", lambda _: ["ollama"])

    assert cost_guard.is_paid_action("esegui_coding_task") is False
    assert cost_guard.estimated_cost("esegui_coding_task") == 0.0
    assert cost_guard.check_cost("esegui_coding_task", "flask_ui") is True


def test_coder_con_fallback_cloud_richiede_consenso_e_costo(monkeypatch, caplog):
    monkeypatch.setattr(model_router, "get_profile", lambda _: {"provider": "ollama"})
    monkeypatch.setattr(
        model_router, "get_provider_chain", lambda _: ["ollama", "gemini"]
    )

    assert cost_guard.is_paid_action("esegui_coding_task") is True
    assert cost_guard.estimated_cost("esegui_coding_task") == 0.10
    assert cost_guard.check_cost("esegui_coding_task", "flask_ui") is False
    assert "$0.10" in caplog.text


def test_backend_opencode_non_viene_assunto_gratuito(monkeypatch):
    monkeypatch.setenv("CLIO_CODING_AGENT", "opencode")
    monkeypatch.setattr(model_router, "get_profile", lambda _: {"provider": "ollama"})
    monkeypatch.setattr(model_router, "get_provider_chain", lambda _: ["ollama"])

    assert cost_guard.is_paid_action("esegui_coding_task") is True
