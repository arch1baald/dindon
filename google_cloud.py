import importlib
import logging
import os
from dataclasses import dataclass
from typing import Dict, List

import pkg_resources
importlib.reload(pkg_resources)  # HACK: https://github.com/googleapis/google-api-python-client/issues/476#issuecomment-371797043
from dialogflow_v2 import SessionsClient, ContextsClient
from dialogflow_v2.types import (
    QueryInput, TextInput, EventInput, InputAudioConfig, QueryParameters, Context, DetectIntentResponse,
)
from discord import Member
from google.cloud import texttospeech, speech_v1p1beta1, translate_v2
from google.protobuf.struct_pb2 import Struct

from utils import sync_to_async, contexts_var, Audio, language, EmptyUtterance

logger = logging.getLogger(__name__)


def get_lang():
    # return dict(ru='ru_RU', en='en_IN')[language.get()]
    return 'ru_RU'


async def text_to_speech(text) -> Audio:
    voice = texttospeech.VoiceSelectionParams(
        ssml_gender=texttospeech.SsmlVoiceGender.FEMALE,
        # language_code='en-US',
        # name='en-IN-Wavenet-B',
        language_code='ru-RU',
        name='ru-RU-Wavenet-C',
    )
    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)
    rate = 48000
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.LINEAR16,
        sample_rate_hertz=rate,
    )
    response = await sync_to_async(
        client.synthesize_speech, input=synthesis_input, voice=voice, audio_config=audio_config,
    )
    result = Audio.from_wav(response.audio_content)
    logger.debug(f"TTS: {text} -> {result}")
    return result


async def speech_to_text(audio: Audio):
    client = speech_v1p1beta1.SpeechClient()

    # TODO: replace with AUDIO_ENCODING_OGG_OPUS
    encoding = speech_v1p1beta1.types.RecognitionConfig.AudioEncoding.LINEAR16
    config = dict(
        language_code=get_lang(),
        sample_rate_hertz=audio.rate,
        encoding=encoding,
    )
    audio = dict(content=audio.to_mono().data)

    response = await sync_to_async(client.recognize, config=config, audio=audio)
    if not response.results:
        raise EmptyUtterance
    transcript = response.results[0].alternatives[0].transcript
    logger.info(f"STT: {response} {transcript}")
    return transcript


@dataclass
class Intent:
    query_text: str
    text: str
    parameters: Dict[str, str]
    action: str
    name: str
    all_required_params_present: bool
    output_contexts: List[str]
    input_contexts: List[str]
    response: DetectIntentResponse = None


def make_parameters(params: dict):
    parameters = Struct()
    parameters.update(params)
    return parameters


async def detect_intent(
    user: Member, text: str = None, speech: Audio = None, event: str = None, params: dict = None,
) -> Intent:
    dialogflow_project_id = os.getenv('DIALOGFLOW_PROJECT')
    client = SessionsClient()
    contexts_client = ContextsClient()
    session = client.session_path(dialogflow_project_id, user)
    logger.debug(f"Session: {session}")
    kwargs = {}
    if text:
        query_input = QueryInput(text=TextInput(text=text, language_code=get_lang()))
    elif speech:
        query_input = QueryInput(audio_config=InputAudioConfig(
            audio_encoding=client.enums.AudioEncoding.AUDIO_ENCODING_LINEAR_16,
            sample_rate_hertz=speech.rate,
            language_code=get_lang(),
            enable_word_info=True,
        ))
        kwargs = dict(input_audio=speech.to_mono().data)
    elif event:
        query_input = QueryInput(event=EventInput(name=event, parameters=make_parameters(params), language_code=get_lang()))
    else:
        raise ValueError("One of `text`, `speech` or `event` should be set")

    query_params = QueryParameters(
        contexts=[
            Context(name=contexts_client.context_path(dialogflow_project_id, user, context), lifespan_count=1)
            for context in contexts_var.get()
        ],
        reset_contexts=True,
    )

    response = await sync_to_async(
        client.detect_intent,
        session=session,
        query_input=query_input,
        query_params=query_params,
        **kwargs,
    )
    intent = Intent(
        text=response.query_result.fulfillment_text,
        parameters={field_name: field.string_value for field_name, field in response.query_result.parameters.fields.items()},
        action=response.query_result.action,
        all_required_params_present=response.query_result.all_required_params_present,
        query_text=response.query_result.query_text,
        name=response.query_result.intent.name,
        output_contexts=[c.name for c in response.query_result.output_contexts],
        input_contexts=contexts_var.get(),
    )
    logger.info(f"Detected intent: {intent}")
    return intent


async def translate(source: str, target: str, text: str):
    translate_client = translate_v2.Client()
    result = await sync_to_async(translate_client.translate, text, source_language=source, target_language=target)
    translated = result['translatedText']
    logger.debug(f"Translated: '{text}' -> '{translated}'")
    return translated


def text_to_speech(text):
    voice = texttospeech.VoiceSelectionParams(
        ssml_gender=texttospeech.SsmlVoiceGender.FEMALE,
        # language_code='en-US',
        # name='en-IN-Wavenet-B',
        language_code='ru-RU',
        name='ru-RU-Wavenet-C',
    )
    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)
    rate = 48000
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.LINEAR16,
        sample_rate_hertz=rate,
    )
    response = client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
    result = Audio.from_wav(response.audio_content)
    logger.debug(f"TTS: {text} -> {result}")
    return result


if __name__ == '__main__':
    text_to_speech('Hello, World!')
