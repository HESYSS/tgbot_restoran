"""Optional Groq speech adapter. Audio stays in memory or short-lived temp files."""
import asyncio
import io
import os
import re
from pathlib import Path
import subprocess
import tempfile
import wave

import httpx

MAX_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 120


class VoiceError(ValueError):
    pass


def chunks(text, limit=200):
    words = text.split()
    if not words or len(text) > 3000 or any(len(word) > limit for word in words):
        raise VoiceError('Unsupported speech text length')
    result, current = [], ''
    for word in words:
        candidate = (current + ' ' + word).strip()
        if len(candidate) > limit:
            result.append(current)
            current = word
        else:
            current = candidate
    result.append(current)
    return result


def join_wav(parts):
    output = io.BytesIO()
    with wave.open(output, 'wb') as destination:
        expected = None
        for part in parts:
            with wave.open(io.BytesIO(part), 'rb') as source:
                params = (source.getnchannels(), source.getsampwidth(), source.getframerate())
                if expected is None:
                    expected = params
                    destination.setnchannels(params[0])
                    destination.setsampwidth(params[1])
                    destination.setframerate(params[2])
                elif params != expected:
                    raise VoiceError('Incompatible speech audio chunks')
                destination.writeframes(source.readframes(source.getnframes()))
    return output.getvalue()


async def to_opus(wav):
    from imageio_ffmpeg import get_ffmpeg_exe
    with tempfile.TemporaryDirectory(prefix='booking-voice-') as directory:
        source, target = Path(directory)/'input.wav', Path(directory)/'reply.ogg'
        source.write_bytes(wav)
        process = await asyncio.create_subprocess_exec(
            get_ffmpeg_exe(), '-nostdin', '-v', 'error', '-i', str(source),
            '-map_metadata', '-1', '-ac', '1', '-ar', '48000', '-c:a', 'libopus',
            '-b:a', '32k', '-t', str(MAX_SECONDS), str(target),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            await asyncio.wait_for(process.wait(), timeout=45)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode or not target.exists():
            raise VoiceError('Audio conversion failed')
        result = target.read_bytes()
        if not result.startswith(b'OggS') or len(result) > MAX_BYTES:
            raise VoiceError('Invalid converted audio')
        return result


class GroqSpeech:
    def __init__(self, key, mode='auto'):
        self.key, self.mode = key, mode
        self.tts_available = True

    async def transcribe(self, audio):
        if not audio or len(audio) > MAX_BYTES:
            raise VoiceError('Voice message is empty or too large')
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post('https://api.groq.com/openai/v1/audio/transcriptions',
                headers={'Authorization': 'Bearer ' + self.key},
                files={'file': ('voice.ogg', audio, 'audio/ogg')},
                data={'model': 'whisper-large-v3', 'temperature': '0', 'response_format': 'verbose_json'})
        response.raise_for_status()
        result = response.json()
        text = result.get('text', '').strip()
        segments = result.get('segments', [])
        if (not text or len(text) > 4000 or result.get('duration', 0) > MAX_SECONDS
                or not segments or any(segment.get('no_speech_prob', 0) > .6
                    or segment.get('avg_logprob', 0) < -1
                    or segment.get('compression_ratio', 0) > 2.4 for segment in segments)):
            raise VoiceError('Uncertain transcription; request a text version')
        # Whisper commonly renders "5 p.m."; existing time guards expect "5 pm".
        return re.sub(r'\b([ap])\.\s*m\.', lambda match: match[1].lower() + 'm', text, flags=re.I)

    async def synthesize(self, text):
        parts = []
        async with httpx.AsyncClient(timeout=60) as client:
            for part in chunks(text):
                response = await client.post('https://api.groq.com/openai/v1/audio/speech',
                    headers={'Authorization': 'Bearer ' + self.key},
                    json={'model': 'canopylabs/orpheus-v1-english', 'voice': 'troy',
                          'input': part, 'response_format': 'wav'})
                if response.status_code in {400, 401, 403, 429}:
                    # Do not hammer a disabled model or exhausted free quota.
                    self.tts_available = False
                response.raise_for_status()
                parts.append(response.content)
        wav = join_wav(parts)
        with wave.open(io.BytesIO(wav), 'rb') as audio:
            if audio.getnframes() / audio.getframerate() > MAX_SECONDS:
                raise VoiceError('Generated speech is too long')
        return await to_opus(wav)


def configured_speech(mode=None):
    mode = mode or os.getenv('VOICE_MODE', 'off').lower()
    if mode not in {'off', 'auto', 'always'}:
        raise ValueError('VOICE_MODE must be off, auto or always')
    if mode == 'off':
        return None
    key = os.getenv('GROQ_API_KEY', '')
    if not key:
        raise ValueError('Voice mode requires GROQ_API_KEY')
    return GroqSpeech(key, mode)
