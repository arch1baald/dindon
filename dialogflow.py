import argparse
import uuid
import os

import dialogflow_v2 as dialogflow

DIALOGFLOW_PROJECT = os.environ['DIALOGFLOW_PROJECT']
DIALOGFLOW_LANGUAGE = os.environ['DIALOGFLOW_LANGUAGE']


def process_text(session_id, text, project_id=DIALOGFLOW_PROJECT, language_code=DIALOGFLOW_LANGUAGE):
    session_client = dialogflow.SessionsClient()
    session = session_client.session_path(project_id, session_id)
    text_input = dialogflow.types.TextInput(text=text, language_code=language_code)
    query_input = dialogflow.types.QueryInput(text=text_input)
    response = session_client.detect_intent(session=session, query_input=query_input)
    return response


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        '--project-id',
        help='Project/agent id.  Required.',
        default=DIALOGFLOW_PROJECT)
    parser.add_argument(
        '--session-id',
        help='Identifier of the DetectIntent session. '
        'Defaults to a random UUID.',
        default=str(uuid.uuid4()))
    parser.add_argument(
        '--language-code',
        help='Language code of the query. Defaults to "en-US".',
        default=DIALOGFLOW_LANGUAGE)
    parser.add_argument(
        'texts',
        # nargs='+',
        type=str,
        help='Text inputs.')

    args = parser.parse_args()
    response = process_text(args.session_id, args.texts)
    print(response.query_result.action)
    print(response)
