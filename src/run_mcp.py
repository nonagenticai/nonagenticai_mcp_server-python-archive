"""Start the same probes-only ASGI application as the container."""

import uvicorn


def main() -> None:
    uvicorn.run("src.asgi:app", host="127.0.0.1", port=8033)


if __name__ == "__main__":
    main()
