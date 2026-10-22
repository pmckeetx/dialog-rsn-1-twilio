"""Call the assistant without a phone: plays the part of Twilio Media Streams against main.py.

    uv run fake_call.py

Speaks three lines as the caller (macOS `say`), plays Rosa's replies at phone speed the way Twilio
would, talks over her second answer to check barge-in, and saves the call to call.wav: caller on the
left channel, Rosa on the right.
"""

import argparse
import asyncio
import audioop
import base64
import json
import subprocess
import tempfile
import time
import wave
from pathlib import Path

import websockets

FRAME = 160  # 20 ms of 8 kHz mu-law, what Twilio sends
SILENCE = b"\xff" * FRAME  # mu-law silence

LINES = [
    "Hi, what are your hours on Saturday?",
    "How much is an emergency callout at night, and what areas do you cover?",
]
BARGE_IN = "Sorry, actually, just the price is fine."


def say(text: str) -> bytes:
    """Synthesize a caller line as 8 kHz mu-law with macOS `say`."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "line.wav"
        subprocess.run(["say", "-v", "Samantha", "-o", str(path), "--data-format=LEI16@8000", text], check=True)
        with wave.open(str(path)) as w:
            return audioop.lin2ulaw(w.readframes(w.getnframes()), 2)


class Phone:
    def __init__(self, ws):
        self.ws = ws
        self.outgoing = bytearray()  # caller audio still to send
        self.playback = bytearray()  # agent audio Twilio would still be playing
        self.left = bytearray()
        self.right = bytearray()
        self.last_media = 0.0
        self.clears = []
        self.started = time.monotonic()

    def now(self):
        return time.monotonic() - self.started

    async def tick(self):
        """Every 20 ms: send one frame of caller audio and play one frame of agent audio."""
        next_at = time.monotonic()
        while True:
            frame, self.outgoing[:] = bytes(self.outgoing[:FRAME]), self.outgoing[FRAME:]
            frame = frame.ljust(FRAME, b"\xff")
            await self.ws.send(json.dumps({
                "event": "media",
                "streamSid": "MZfake",
                "media": {"track": "inbound", "payload": base64.b64encode(frame).decode()},
            }))
            heard, self.playback[:] = bytes(self.playback[:FRAME]), self.playback[FRAME:]
            self.left += frame
            self.right += heard.ljust(FRAME, b"\xff")
            next_at += 0.02
            await asyncio.sleep(max(0, next_at - time.monotonic()))

    async def receive(self):
        async for message in self.ws:
            event = json.loads(message)
            if event["event"] == "media":
                self.playback += base64.b64decode(event["media"]["payload"])
                self.last_media = time.monotonic()
            elif event["event"] == "clear":
                dropped = len(self.playback) / 8000
                self.clears.append((self.now(), dropped))
                print(f"{self.now():6.2f}s  clear: dropped {dropped * 1000:.0f} ms of queued audio")
                self.playback.clear()

    def speaking(self):
        return bool(self.playback)

    async def wait_for_reply(self, timeout=20.0):
        """Wait until Rosa starts talking, then until she has finished."""
        deadline = time.monotonic() + timeout
        while not self.speaking():
            if time.monotonic() > deadline:
                raise TimeoutError("no reply")
            await asyncio.sleep(0.05)
        first = self.now()
        while self.speaking() or time.monotonic() - self.last_media < 1.0:
            await asyncio.sleep(0.05)
        return first

    async def talk(self, audio: bytes, label: str):
        print(f"{self.now():6.2f}s  caller: {label}")
        self.outgoing += audio
        while self.outgoing:
            await asyncio.sleep(0.02)
        return self.now()


async def main(url: str, out: str):
    lines = [say(text) for text in LINES]
    barge_in = say(BARGE_IN)

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        await ws.send(json.dumps({
            "event": "start",
            "streamSid": "MZfake",
            "start": {
                "streamSid": "MZfake",
                "callSid": "CAfake",
                "tracks": ["inbound"],
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            },
        }))
        phone = Phone(ws)
        tasks = [asyncio.create_task(phone.tick()), asyncio.create_task(phone.receive())]

        first = await phone.wait_for_reply()
        print(f"{first:6.2f}s  Rosa answers (greeting)")

        done = await phone.talk(lines[0], LINES[0])
        first = await phone.wait_for_reply()
        print(f"{first:6.2f}s  Rosa replies, {first - done:.2f}s after the caller's audio ended")

        done = await phone.talk(lines[1], LINES[1])
        while not phone.speaking():
            await asyncio.sleep(0.05)
        print(f"{phone.now():6.2f}s  Rosa replies, {phone.now() - done:.2f}s after the caller's audio ended")
        await asyncio.sleep(2.0)
        await phone.talk(barge_in, BARGE_IN + "  (talking over Rosa)")
        first = await phone.wait_for_reply()
        print(f"{first:6.2f}s  Rosa replies to the interruption")

        await ws.send(json.dumps({"event": "stop", "streamSid": "MZfake"}))
        await asyncio.sleep(0.5)
        for task in tasks:
            task.cancel()

    with wave.open(out, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(8000)
        left = audioop.ulaw2lin(bytes(phone.left), 2)
        right = audioop.ulaw2lin(bytes(phone.right), 2)
        n = min(len(left), len(right))
        w.writeframes(audioop.add(audioop.tostereo(left[:n], 2, 1, 0), audioop.tostereo(right[:n], 2, 0, 1), 2))
    barged = any(dropped > 0 for _, dropped in phone.clears)
    print(f"saved {out}; barge-in {'cleared playback' if barged else 'did NOT clear playback'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://localhost:5050/media-stream")
    parser.add_argument("--out", default="call.wav")
    args = parser.parse_args()
    asyncio.run(main(args.url, args.out))
