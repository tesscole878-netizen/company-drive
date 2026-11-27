import os
from pathlib import Path
import uvicorn
from drive.app import create_app
from drive.storage_config import configure_storage

configure_storage(Path(__file__).resolve().parent)
app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("DRIVE_HOST", "127.0.0.1"),
                port=int(os.environ.get("DRIVE_PORT", "8765")),
                proxy_headers=os.environ.get("DRIVE_TRUST_PROXY", "0") == "1")
