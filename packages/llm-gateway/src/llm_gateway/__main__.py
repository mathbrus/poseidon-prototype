"""LLM gateway entry point:

    python -m llm_gateway

Configured by env (see `llm_gateway.config`); listens on the platform's
LLM gateway port.
"""

import uvicorn

from common.topology import LLM_GATEWAY_PORT
from llm_gateway.app import create_app


def main() -> None:
    uvicorn.run(create_app(), host="0.0.0.0", port=LLM_GATEWAY_PORT)


if __name__ == "__main__":
    main()
