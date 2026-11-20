"""Synthetic speech round trip; never connects to Telegram or sends a booking."""
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from .voice import GroqSpeech


async def check():
    load_dotenv(override=True)
    speech = GroqSpeech(os.environ['GROQ_API_KEY'])
    phrase = 'This is Fictional Test Bistro. We have a table for two available at five pm.'
    audio = await speech.synthesize(phrase)
    output = Path('data/voice-check.ogg')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(audio)
    transcript = await speech.transcribe(audio)
    print('Generated OGG Opus:', len(audio), 'bytes')
    print('Synthetic input:', phrase)
    print('Recognised:', transcript)
    print('Audio saved:', output.resolve())


if __name__ == '__main__':
    asyncio.run(check())
