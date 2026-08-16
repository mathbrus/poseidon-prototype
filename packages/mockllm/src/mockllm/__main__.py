"""Entry point matching the llm-proxy Deployment's LiteLLM-shaped args:

    python -m mockllm --model <ignored> --port 4000 --drop_params

so the Helm template's args work verbatim whether the pod runs LiteLLM
or this mock.
"""

import argparse

import uvicorn

from mockllm.app import app


def main() -> None:
    parser = argparse.ArgumentParser("mockllm")
    parser.add_argument("--model", default="mock-brain", help="accepted and ignored")
    parser.add_argument("--port", type=int, default=4000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--drop_params",
        action="store_true",
        help="LiteLLM flag; accepted and ignored",
    )
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
