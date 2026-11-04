class SDKProviderConfig:
    pass
class ClaudeSDKProvider:
    @staticmethod
    def _session_key(message):
        return (message.get("meta") or {}).get("api_session") or "demo"
