
def main() -> int:
    import sys
    import os

    # Ensure the workspace is in the python path
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))

    from clio_core import elabora_richiesta, safe_client

    print("🚀 Starting test for 'apri youtube' with forced Gemini failover...")

    # Mock Groq to fail instantly to test Gemini failover
    class MockGroq:
        class chat:
            class completions:
                @staticmethod
                def create(*args, **kwargs):
                    print("🔥 [MOCK] Groq failing immediately...")
                    raise Exception("Mock Groq Rate Limit Exception")

    if safe_client:
        safe_client.groq = MockGroq()
        print("✅ Groq mocked for failure.")
    else:
        print("⚠️ safe_client not initialized?")

    try:
        # We call elabora_richiesta directly
        res = elabora_richiesta("apri youtube")
        print("\n✅ Result from elabora_richiesta:")
        print(res)
    except Exception as e:
        print(f"\n❌ Exception caught in test script: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    raise SystemExit(main())
