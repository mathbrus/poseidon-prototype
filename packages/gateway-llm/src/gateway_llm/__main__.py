"""LLM gateway entry point:

    python -m gateway_llm

Configured by env (see `gateway_llm.config`); listens on LLM_GATEWAY_PORT
(default 4000 — the port agents already expect for model access).
"""

import os

import uvicorn

from gateway_llm.app import create_app


def main() -> None:
    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=int(os.environ.get("LLM_GATEWAY_PORT", 4000)),
    )


if __name__ == "__main__":
    main()
