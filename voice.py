import asyncio
import logging
import time
from typing import List
from struct import Struct
from contextlib import suppress, asynccontextmanager

from discord import VoiceClient, SpeakingState
from discord.reader import AudioSink, AudioReader
from discord.opus import Decoder, Encoder
from webrtcvad import Vad
from pvporcupine import create

import google_cloud
import google_cloud as yandex  # FIXME: Get yandex API key
from utils import set_contexts, BackgroundTask, background_task, registry, language, Audio, EmptyUtterance
from google_cloud import detect_intent


logger = logging.getLogger(__name__)


class UserSink:
    def __init__(self, parent, keywords, user):
        self.parent = parent
        self.user = user
        self.pcp = create(keywords=keywords, sensitivities=[0.5] * len(keywords))
        self.unpacker = Struct(f'{self.pcp.frame_length}h')
        self.input_queue = asyncio.Queue()

        self.what = parent.what
        self.that = parent.that
        self.ticktock = parent.ticktock
        self.keywords = parent.keywords
        self.play = parent.play
        self.play_loop = parent.play_loop
        self.play_stream = parent.play_stream
        self.play_interruptible = parent.play_interruptible
        self.speak = parent.speak
        self.vad = Vad(3)
        self.listen_task = BackgroundTask()
        self.listen_task.start(self.listen_loop())
        logger.info(f"Started {self!r}")

    def __repr__(self):
        return f'{type(self).__name__}<{self.user}>'

    def detect_wuw(self, sound: Audio):
        result = self.pcp.process(self.unpacker.unpack(sound.data))
        if result >= 0:
            return self.keywords[result]

    async def process_wakeup(self):
        try:
            utterance: Audio = await self.listen_audio()
            with set_contexts():
                await self.process_utterance(utterance)
        except EmptyUtterance:
            await self.speak("В следующий раз я тоже тебе не отвечу!")
        except Interrupted:
            logger.info(f"{self!r} interrupted")

    async def wait_for_wuw(self) -> str:
        """Listen for wake up word and return it"""
        sound = Audio(rate=self.pcp.sample_rate, channels=1, width=2)
        while True:
            sound += (await self.listen()).to_mono().to_rate(self.pcp.sample_rate)
            while len(sound) >= self.pcp.frame_length:
                to_process = sound[:self.pcp.frame_length]
                sound = sound[self.pcp.frame_length:]
                keyword = self.detect_wuw(to_process)
                if keyword:
                    logger.info(f'Detected keyword "{keyword}"')
                    return keyword

    async def listen_loop(self):
        while True:
            try:
                await self.wait_for_wuw()
                async with self.parent.attention:
                    await self.process_wakeup()
            except Exception as exc:
                logger.exception(f"Unexpected exception in {self!r}.listen_loop: {exc}")

    async def listen_audio(self, timeout=2.3) -> Audio:
        logger.info(f"{self!r} start listening utterance with timeout {timeout}")
        async with background_task(self.play_loop(self.ticktock)):
            sound = Audio(channels=Decoder.CHANNELS, width=2, rate=Decoder.SAMPLING_RATE)
            speech_count = 0
            speech_threshold = 10
            self.input_queue = asyncio.Queue()
            while True:
                try:
                    actual_timeout = 0.4 if speech_count >= speech_threshold else timeout
                    packet = await asyncio.wait_for(self.listen(), timeout=actual_timeout)
                    sound += packet
                    is_speech = self.vad.is_speech(packet.to_mono().to_rate(16000).data, 16000)
                    logger.debug(f"{self!r} speech packet: is_speech={is_speech} rms={packet.rms}")
                    if is_speech and packet.rms > 50:
                        speech_count += 1
                except asyncio.TimeoutError:
                    logger.info(f"{self!r} stop listening utterance")
                    if len(sound) == 0:
                        raise EmptyUtterance()
                    logger.info(f"{self!r} listened speech: {sound.duration}s", extra=dict(speech=sound))
                    return sound

    async def listen(self) -> Audio:
        return await self.input_queue.get()

    async def feed(self, audio: Audio):
        self.input_queue.put_nowait(audio)
        logger.debug(f'feed: {self!r}. qsize={self.input_queue.qsize()}')

    async def process_utterance(self, utterance: Audio):
        text = await speech_to_text(utterance)
        if not text:
            raise EmptyUtterance()
        intent = await detect_intent(self.user, text)
        query = intent.parameters.get('query', text)
        if intent.text:
            await self.speak(intent.text)
            if not intent.all_required_params_present:
                user_answer: Audio = await self.listen_audio()
                await self.process_utterance(user_answer)
        if intent.action == 'format':
            answer = intent.text.format(query=query)
            await self.speak(answer)
        elif intent.action.startswith('skill.'):
            skill = intent.action.split('skill.')[-1]
            await registry.run_skill(skill, self, self.user, **intent.parameters)
        elif intent.action == 'set-language':
            language.set(intent.parameters['lang'])
        elif intent.action == 'fallback':
            await registry.run_skill('parlai', self, self.user, initial=intent.query_text)
        elif intent.action:
            logger.warning(f"{self!r} unknown action {intent.action}")
            await self.speak(f"неизвестный экшен: {intent.action}")

    async def listen_text(self, timeout=4, tries=1):
        while True:
            try:
                speech = await self.listen_audio(timeout=timeout)
                text = await speech_to_text(speech)
                if not text:
                    raise EmptyUtterance()
                return text
            except EmptyUtterance:
                tries -= 1
                if tries:
                    await self.speak("Ну что же ты молчишь?")
                else:
                    raise

    async def ask(self, question, timeout=4, tries=1):
        request = await text_to_speech(text=question)
        await self.play(request)
        return await self.listen_text(timeout, tries)

    async def ask_and_detect_intent(self, question, timeout=4, tries=3):
        request = await text_to_speech(text=question)
        await self.play(request)
        while tries >= 0:
            tries -= 1
            try:
                speech = await self.listen_audio(timeout=timeout)
            except EmptyUtterance:
                await self.speak("Ну что же ты молчишь?")
            else:
                intent = await detect_intent(self.user, speech=speech)
                if intent.action == 'repeat':
                    await self.play(request)
                    tries += 1
                elif intent.action == 'fallback':
                    await self.speak(intent.text)
                else:
                    return intent
        else:
            raise EmptyUtterance

    async def ask_yes_no(self, question: str):
        with set_contexts('yes-no'):
            intent = await self.ask_and_detect_intent(question)
            return intent.action == 'yes'

    async def close(self):
        self.pcp.delete()

    async def on_welcome(self):
        logger.info(f"Welcome {self.user}")
        intent = await detect_intent(self.user, event='WELCOME', params=dict(name=self.user.name))
        if intent.text:
            await self.speak(intent.text)


class DemultiplexerSink(AudioSink):
    """
    https://ru.wikipedia.org/wiki/%D0%94%D0%B5%D0%BC%D1%83%D0%BB%D1%8C%D1%82%D0%B8%D0%BF%D0%BB%D0%B5%D0%BA%D1%81%D0%BE%D1%80

    """
    def __init__(self, voice_client: VoiceClient, keywords: List[str]):
        self.client = voice_client
        self.keywords = keywords
        self.users = {}
        self.deleted = False
        self.is_speaking = False
        self.what = Audio.load('what2.wav')
        self.that = Audio.load('that2.wav')
        self.hello = Audio.load('hello.wav')
        self.ticktock = Audio.load('ticktock.wav')
        self.wakeups = asyncio.Queue()
        self.reader = AudioReader(self, voice_client)
        self.demux_task = BackgroundTask()
        self.attention = asyncio.Lock()
        self.speak_lock = asyncio.Lock()

    def __str__(self):
        return f'channel={self.client.channel}'

    def __repr__(self):
        return f'<{type(self).__name__}>[{self}]'

    async def start(self):
        self.demux_task.start(self.demux_loop())
        logger.info(f"Started {self!r}")
        await self.play(self.hello)

    async def cleanup(self):
        # TODO: move to __aenter__/__aexit__
        if self.deleted:
            return
        self.deleted = True
        logger.debug(f"Deleting {self!r}")
        for usersink in self.users.values():
            await usersink.close()

    async def demux_loop(self):
        logger.info(f'''
        Running demux loop...
        self.reader.listen_voice(): {self.reader.listen_voice()}

        If empty
        pipenv install "discord.py[voice] @ git+https://github.com/nikicat/discord.py@voice-recv-nb-merged"
        ''')
        async for voice_data in self.reader.listen_voice():
            try:
                sink = self.get_user_sink(voice_data.user)
                audio = Audio(data=voice_data.pcm, channels=2, width=2, rate=48000)
                await sink.feed(audio)
            except Exception as exc:
                logger.exception(f"Unexpected exception in {self!r}.demux_loop: {exc}")

    def get_user_sink(self, user):
        if user not in self.users:
            self.users[user] = UserSink(self, self.keywords, user)
        return self.users[user]

    async def play_stream(self, stream):
        return await self.run_interruptible(self.play_stream_impl(stream))

    async def play_stream_impl(self, stream):
        async with self.speaking():
            async for frame in rate_limit(size_limit(stream, Encoder.SAMPLES_PER_FRAME)):
                await self.send_packet(frame)

    async def play(self, sound: Audio):
        async with self.speaking():
            async for packet in rate_limit(size_limit(aiter([sound]), Encoder.SAMPLES_PER_FRAME)):
                if len(packet) < Encoder.SAMPLES_PER_FRAME:
                    # To avoid "clatz" in the end
                    packet += packet.silence(Encoder.SAMPLES_PER_FRAME - len(packet))
                await self.send_packet(packet)

    async def send_packet(self, packet: Audio):
        self.client.send_audio_packet(packet.to_stereo().to_rate(Encoder.SAMPLING_RATE).data)

    async def play_loop(self, sound: Audio):
        async with self.speaking():
            while True:
                await self.play(sound)

    async def run_interruptible(self, coro):
        logger.debug("Playing interruptible")
        # FIXME: listen for users those entered chat after start
        waiters = {sink.wait_for_wuw(): user for user, sink in self.users.items()}
        done, pending = await asyncio.wait(list(waiters) + [coro], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        done_coro = done.pop().get_coro()
        if done_coro in waiters:
            logger.debug(f"Interrupted by {waiters[done_coro]}")
            raise Interrupted

    async def play_interruptible(self, audio: Audio):
        return await self.run_interruptible(self.play(audio))

    async def speak(self, text):
        logger.debug(f"Speaking: {text}")
        audio = await text_to_speech(text=text)
        await self.play_interruptible(audio)

    @asynccontextmanager
    async def speaking(self):
        if self.is_speaking:
            yield
        else:
            try:
                async with self.speak_lock:
                    await self.client.ws.speak(SpeakingState.active())
                    self.is_speaking = True
                    yield
            finally:
                self.is_speaking = False
                await self.client.ws.speak(SpeakingState.inactive())

    async def on_welcome(self, user):
        await self.get_user_sink(user).on_welcome()


async def speech_to_text(audio: Audio):
    if language.get() == 'ru':
        return await yandex.speech_to_text(audio)
    else:
        return await google_cloud.speech_to_text(audio)


async def text_to_speech(text: str):
    if language.get() == 'ru':
        return await yandex.text_to_speech(text)
    else:
        return await google_cloud.text_to_speech(text)


async def aiter(iter_):
    for i in iter_:
        yield i


async def size_limit(audio_iter, size):
    buf = None
    async for packet in audio_iter:
        buf = buf + packet if buf else packet
        while len(buf) >= size:
            yield buf[:size]
            buf = buf[size:]
    if buf:
        yield buf


async def rate_limit(audio_iter):
    when_to_wake = time.perf_counter()
    async for packet in audio_iter:
        yield packet
        when_to_wake += packet.duration
        to_sleep = when_to_wake - time.perf_counter()
        await asyncio.sleep(to_sleep)
    if packet:
        yield packet


class Interrupted(Exception):
    pass
