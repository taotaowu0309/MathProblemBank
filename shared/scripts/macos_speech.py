"""Play the existing Edge TTS voice through macOS afplay, without mpv."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import signal
import subprocess
import tempfile

import edge_tts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", required=True)
    parser.add_argument("--rate", default="+0%")
    parser.add_argument("--text", required=True)
    args = parser.parse_args()
    player: subprocess.Popen[bytes] | None = None

    def stop(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    with tempfile.TemporaryDirectory(prefix="mpb-speech-") as temporary:
        target = Path(temporary) / "speech.mp3"
        try:
            asyncio.run(edge_tts.Communicate(args.text, args.voice, rate=args.rate).save(str(target)))
            player = subprocess.Popen(["/usr/bin/afplay", str(target)])
            return player.wait()
        finally:
            if player is not None and player.poll() is None:
                player.terminate()
                player.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
