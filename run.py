"""Start the local replay copy. Python 3.12+."""
import os, socket, json
from pathlib import Path
root = Path(__file__).resolve().parent
os.chdir(root)
settings=json.loads((root/"settings.json").read_text(encoding="utf-8"))
# Do not inherit production integration settings.
for key in list(os.environ):
    if key.startswith(("RELAY_", "OB_", "GATEWAY_", "LOOP_", "SDK_", "LOCAL_", "MINIMAX_", "MOSS_", "STT_", "TTS_")):
        os.environ.pop(key)
(root / "data").mkdir(exist_ok=True)
os.environ.update(RELAY_DB_PATH=str(root/"data/demo.db"), RELAY_UPLOAD_DIR=str(root/"data/uploads"), RELAY_VOICE_DIR=str(root/"data/voice"), RELAY_BRAIN_FILE=str(root/"data/provider"), RELAY_AVATAR_CONFIG=str(root/"data/identity.json"), RELAY_HOST="127.0.0.1", RELAY_SECRET="demo-local", AI=settings["AI"], USER=settings["USER"])
# The replay build has no outbound network capability, even if an old endpoint is clicked.
_original_getaddrinfo = socket.getaddrinfo
def local_address(host, *args, **kwargs):
    if host not in (None, "localhost", "127.0.0.1", "::1"):
        raise OSError("Offline replay: external lookup disabled")
    return _original_getaddrinfo(host,*args,**kwargs)
socket.getaddrinfo=local_address
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
def local_connect(self, address):
    if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1", "localhost"):
        raise OSError("Offline replay: external connections disabled")
    return _original_connect(self, address)
socket.socket.connect = local_connect
def local_connect_ex(self, address):
    if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1", "localhost"):
        raise OSError("Offline replay: external connections disabled")
    return _original_connect_ex(self, address)
socket.socket.connect_ex = local_connect_ex
import uvicorn
uvicorn.run("backend.app:app", host="127.0.0.1", port=int(os.environ.get("DEMO_PORT","8765")))
