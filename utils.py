import os
import re

import dialogflow_v2 as dialogflow


DIALOGFLOW_PROJECT = os.environ['DIALOGFLOW_PROJECT']
DIALOGFLOW_LANGUAGE = os.environ['DIALOGFLOW_LANGUAGE']


def extract_intent(session_id, text, project_id=DIALOGFLOW_PROJECT, language_code=DIALOGFLOW_LANGUAGE):
    session_client = dialogflow.SessionsClient()
    session = session_client.session_path(project_id, session_id)
    text_input = dialogflow.types.TextInput(text=text, language_code=language_code)
    query_input = dialogflow.types.QueryInput(text=text_input)
    response = session_client.detect_intent(session=session, query_input=query_input)
    return response


def remove_mentions(text):
    text = re.sub(r'<@(everyone|here|[!&]?[0-9]{17,21})>', '', text)
    text = text.strip()
    return text
