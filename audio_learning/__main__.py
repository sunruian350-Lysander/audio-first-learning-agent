"""Run the single-user local workspace with ``python -m audio_learning``."""

import uvicorn


def main() -> None:
    uvicorn.run("audio_learning.main:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
