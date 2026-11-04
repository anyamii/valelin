class OutboxConfig:
    enabled = False
    @classmethod
    def from_env(cls): return cls()
class LocalOutbox:
    def __init__(self, config): self.config = config
    async def close(self): pass
def raw_event(*args, **kwargs): return None
