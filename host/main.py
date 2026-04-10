#!/usr/bin/env python3
"""
Fan Control - Host Server Entry Point
"""

import logging
import uvicorn

from config import SERVER_HOST, SERVER_PORT
from server import create_app

# Configure logging
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


def main():
    app = create_app()
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning")


if __name__ == "__main__":
    main()
