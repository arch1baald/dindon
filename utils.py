import audioop
import io

import asyncio
import dialogflow_v2 as dialogflow
import os
import logging
import pickle
from _contextvars import ContextVar

import simpleaudio
import wave
from contextlib import contextmanager, suppress, asynccontextmanager
from dataclasses import dataclass
from functools import partial


logger = logging.getLogger(__name__)
contexts_var = ContextVar('contexts', default=[])
language = ContextVar('language', default=os.getenv('DIALOGFLOW_LANGUAGE'))
DIALOGFLOW_PROJECT = os.environ['DIALOGFLOW_PROJECT']
DIALOGFLOW_LANGUAGE = os.environ['DIALOGFLOW_LANGUAGE']


@dataclass
class Audio:
    channels: int
    width: int
    rate: int
    data: bytes = b''

    def __str__(self):
        duration = round(self.duration, 3)
        return f'channels={self.channels} width={self.width} rate={self.rate}Hz frames={len(self)} duration={duration}s'

    def __repr__(self):
        return f'{type(self)}[{self}]'

    def __add__(self, other):
        if other.channels != self.channels or other.width != self.width or other.rate != self.rate:
            raise ValueError("Could not add incompatible Audio")
        return self.clone(self.data + other.data)

    def __mul__(self, factor):
        return self.clone(audioop.mul(self.data, self.width, factor))

    def combine(self, other):
        return self.clone(data=audioop.add(self.data, other.data, self.width))

    def clone(self, data):
        """Return clone with different data"""
        return Audio(width=self.width, channels=self.channels, rate=self.rate, data=data)

    def __getitem__(self, slice):
        """Slice frames"""
        start = slice.start and slice.start * self.framewidth
        stop = slice.stop and slice.stop * self.framewidth
        step = slice.step and slice.step * self.framewidth
        return self.clone(self.data[start:stop:step])

    def __len__(self) -> int:
        """Audio length in frames"""
        return len(self.data) // self.framewidth

    def __bool__(self) -> bool:
        return len(self) > 0

    @property
    def duration(self) -> float:
        """Duration in seconds"""
        return len(self) / self.rate

    @property
    def framewidth(self) -> int:
        return self.channels * self.width

    @property
    def rms(self) -> float:
        return audioop.rms(self.data, self.width)

    def clear(self):
        self.data = b''

    def to_mono(self):
        if self.channels == 1:
            return self
        elif self.channels == 2:
            return Audio(
                channels=1, width=self.width, rate=self.rate,
                data=audioop.tomono(self.data, self.width, 0.5, 0.5)
            )
        else:
            raise ValueError(f"Can't convert audio with channels={self.channels}")

    def to_stereo(self):
        if self.channels == 2:
            return self
        elif self.channels == 1:
            return Audio(
                channels=2, width=self.width, rate=self.rate,
                data=audioop.tostereo(self.data, self.width, 0.5, 0.5)
            )
        else:
            raise ValueError(f"Can't convert audio with channels={self.channels}")

    def to_rate(self, rate) -> 'Audio':
        converted, _ = audioop.ratecv(self.data, self.width, self.channels, self.rate, rate, None)
        return Audio(channels=self.channels, width=self.width, rate=rate, data=converted)

    @classmethod
    def load(cls, fp: str) -> 'Audio':
        with wave.open(fp, 'rb') as f:
            return Audio(
                data=f.readframes(2 ** 32),
                channels=f.getnchannels(),
                width=f.getsampwidth(),
                rate=f.getframerate(),
            )

    @classmethod
    def from_wav(cls, wav: bytes) -> 'Audio':
        return cls.load(io.BytesIO(wav))

    def to_wav(self) -> io.BytesIO:
        wav = io.BytesIO()
        with wave.open(wav, 'wb') as f:
            f.setnchannels(self.channels)
            f.setsampwidth(self.width)
            f.setframerate(self.rate)
            f.writeframes(self.data)
        wav.seek(0)
        return wav

    def silence(self, frames: int) -> 'Audio':
        return self.clone(b'\x00' * (frames * self.framewidth))

    def play(self):
        """Useful for debugging"""
        play = simpleaudio.play_buffer(
            self.data, num_channels=self.channels, bytes_per_sample=self.width, sample_rate=self.rate)
        try:
            play.wait_done()
        except KeyboardInterrupt:
            play.stop()


class Registry:
    def __init__(self):
        self.skills = {}

    def skill(self, name=None):
        def decorator(func):
            skill_name = name or func.__name__
            self.skills[skill_name] = func
            logger.info(f"Loaded skill '{skill_name}'")
            return func

        return decorator

    async def run_skill(self, skill, bot, user, *args, **kwargs):
        if skill in self.skills:
            try:
                state = self.load_state(skill, user)
            except Exception as exc:
                logger.debug(f"Cant load state for {skill}.{user} due to {exc}")
                state = None
            state = await self.skills[skill](bot, state, *args, **kwargs)
            if state:
                self.save_state(skill, user, state)
        else:
            await bot.speak("такого я не умею")

    def get_state_path(self, skill: str, user: str):
        return f'skill-states/{skill}.{user}.pickle'

    def load_state(self, skill: str, user: str):
        with open(self.get_state_path(skill, user), 'rb') as f:
            return pickle.load(f)

    def save_state(self, skill: str, user: str, state):
        with open(self.get_state_path(skill, user), 'wb') as f:
            return pickle.dump(state, f)


registry = Registry()


@contextmanager
def set_contexts(*contexts):
    token = contexts_var.set(contexts)
    try:
        yield
    finally:
        contexts_var.reset(token)


class BackgroundTask:
    def __init__(self):
        self.task = None

    def start(self, coro):
        if self.task is not None:
            raise RuntimeError("{self!r} is already running")
        self.task = asyncio.create_task(coro)

    async def stop(self):
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        self.task = None


@asynccontextmanager
async def background_task(coro):
    try:
        task = BackgroundTask()
        task.start(coro)
        yield
    finally:
        await task.stop()


async def sync_to_async(func, *args, **kwargs):
    return await asyncio.get_running_loop().run_in_executor(None, partial(func, *args, **kwargs))


class EmptyUtterance(Exception):
    pass


def extract_intent(session_id, text, project_id=DIALOGFLOW_PROJECT, language_code=DIALOGFLOW_LANGUAGE):
    session_client = dialogflow.SessionsClient()
    session = session_client.session_path(project_id, session_id)
    text_input = dialogflow.types.TextInput(text=text, language_code=language_code)
    query_input = dialogflow.types.QueryInput(text=text_input)
    response = session_client.detect_intent(session=session, query_input=query_input)
    return response
