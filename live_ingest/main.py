"""Entry point for the live ingest service."""

import uvicorn
from live_ingest.service import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8020, log_level="info")
