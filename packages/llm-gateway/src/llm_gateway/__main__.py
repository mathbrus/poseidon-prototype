"""LLM gateway entry point:

    python -m llm_gateway

Configured by env (see `llm_gateway.config`); listens on LLM_GATEWAY_PORT
(default 4000 — the port agents already expect for model access).
"""

import os

import uvicorn

from llm_gateway.app import create_app


def main() -> None:
    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=int(os.environ.get("LLM_GATEWAY_PORT", 4000)),
    )


if __name__ == "__main__":
    main()
