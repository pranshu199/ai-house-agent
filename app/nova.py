import asyncio
import base64
import json
import os
import uuid
from aws_credentials_imds import IMDSCredentialsResolver
from smithy_http.aio.crt import AWSCRTHTTPClient

from aws_sdk_bedrock_runtime.client import AsyncBedrockRuntimeClient
from aws_sdk_bedrock_runtime.config import AsyncBedrockRuntimeConfig
from aws_sdk_bedrock_runtime.models import (
    InvokeModelWithBidirectionalStreamOperationInput,
    InvokeModelWithBidirectionalStreamInputChunk,
    BidirectionalInputPayloadPart,
)


MODEL_ID = "amazon.nova-2-sonic-v1:0"
REGION = "us-east-1"

SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2


def _track_crt_request_writer_tasks(stream):
    """Keep references to the CRT request-body writer tasks for this stream."""

    writer_tasks = getattr(stream, "_nova_crt_writer_tasks", None)
    if writer_tasks is None:
        writer_tasks = set()
        stream._nova_crt_writer_tasks = writer_tasks

    current_task = asyncio.current_task()
    for task in asyncio.all_tasks():
        if task is current_task:
            continue

        coroutine = task.get_coro()
        if getattr(coroutine, "__qualname__", "").endswith(
            "AIOHttp2ClientStream._set_request_body_generator"
        ):
            writer_tasks.add(task)


async def _close_input_stream_and_wait(stream):
    """Half-close Nova input and retrieve its CRT writer task result."""

    _track_crt_request_writer_tasks(stream)

    input_stream = stream.input_stream
    if not getattr(input_stream, "closed", False):
        await input_stream.close()

    writer_tasks = getattr(stream, "_nova_crt_writer_tasks", set())
    if writer_tasks:
        tasks = tuple(writer_tasks)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        writer_tasks.difference_update(tasks)

        for result in results:
            if isinstance(result, BaseException):
                raise result


async def send_event(stream, event):
    """Send one JSON event to Nova 2 Sonic."""

    payload = json.dumps(event).encode("utf-8")

    await stream.input_stream.send(
        InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(
                bytes_=payload
            )
        )
    )
    _track_crt_request_writer_tasks(stream)


async def create_nova_stream():
    """
    Create and configure a Nova 2 Sonic bidirectional stream.

    Returns:
        client, stream, prompt_name
    """

    print("NOVA DEBUG 1: starting config resolve", flush=True)

    http_client = AWSCRTHTTPClient()

    credential_resolver = IMDSCredentialsResolver(
        http_client=http_client
    )

    config = await AsyncBedrockRuntimeConfig.resolve(
        region=REGION,
        transport=http_client,
        aws_credentials_identity_resolver=credential_resolver,
    )

    print("NOVA DEBUG 2: config resolved", flush=True)

    client = AsyncBedrockRuntimeClient(
        config=config
    )

    print("NOVA DEBUG 3: starting invoke_model_with_bidirectional_stream", flush=True)

    stream = await client.invoke_model_with_bidirectional_stream(
        InvokeModelWithBidirectionalStreamOperationInput(
            model_id=MODEL_ID
        )
    )

    print("NOVA DEBUG 4: stream created", flush=True)

    prompt_name = str(uuid.uuid4())

    return client, stream, prompt_name
async def start_session(stream, prompt_name):
    """Send Nova session and prompt configuration."""

    await send_event(
        stream,
        {
            "event": {
                "sessionStart": {
                    "inferenceConfiguration": {
                        "maxTokens": 1024,
                        "topP": 0.9,
                        "temperature": 0.7,
                    }
                }
            }
        },
    )

    await send_event(
        stream,
        {
            "event": {
                "promptStart": {
                    "promptName": prompt_name,
                    "textOutputConfiguration": {
                        "mediaType": "text/plain"
                    },
                    "audioOutputConfiguration": {
                        "mediaType": "audio/lpcm",
                        "sampleRateHertz": 24000,
                        "sampleSizeBits": 16,
                        "channelCount": 1,
                        "voiceId": "matthew",
                        "encoding": "base64",
                        "audioType": "SPEECH",
                    },
                }
            }
        },
    )


async def send_system_prompt(
    stream,
    prompt_name,
    system_prompt,
):
    """Send the system instructions to Nova."""

    content_name = str(uuid.uuid4())

    await send_event(
        stream,
        {
            "event": {
                "contentStart": {
                    "promptName": prompt_name,
                    "contentName": content_name,
                    "type": "TEXT",
                    "interactive": True,
                    "role": "SYSTEM",
                    "textInputConfiguration": {
                        "mediaType": "text/plain"
                    },
                }
            }
        },
    )

    await send_event(
        stream,
        {
            "event": {
                "textInput": {
                    "promptName": prompt_name,
                    "contentName": content_name,
                    "content": system_prompt,
                }
            }
        },
    )

    await send_event(
        stream,
        {
            "event": {
                "contentEnd": {
                    "promptName": prompt_name,
                    "contentName": content_name,
                }
            }
        },
    )


async def send_audio_content_start(
    stream,
    prompt_name,
):
    """
    Start an interactive user audio content block.

    Returns:
        content_name
    """

    content_name = str(uuid.uuid4())

    await send_event(
        stream,
        {
            "event": {
                "contentStart": {
                    "promptName": prompt_name,
                    "contentName": content_name,
                    "type": "AUDIO",
                    "interactive": True,
                    "role": "USER",
                    "audioInputConfiguration": {
                        "mediaType": "audio/lpcm",
                        "sampleRateHertz": SAMPLE_RATE,
                        "sampleSizeBits": 16,
                        "channelCount": CHANNELS,
                        "audioType": "SPEECH",
                        "encoding": "base64",
                    },
                }
            }
        },
    )

    return content_name


async def send_audio_chunk(
    stream,
    prompt_name,
    content_name,
    audio_bytes,
):
    """Send one PCM audio chunk to Nova."""

    encoded_audio = base64.b64encode(
        audio_bytes
    ).decode("utf-8")

    await send_event(
        stream,
        {
            "event": {
                "audioInput": {
                    "promptName": prompt_name,
                    "contentName": content_name,
                    "content": encoded_audio,
                }
            }
        },
    )


async def end_audio_content(
    stream,
    prompt_name,
    content_name,
):
    """Tell Nova that the current user audio content has ended."""

    await send_event(
        stream,
        {
            "event": {
                "contentEnd": {
                    "promptName": prompt_name,
                    "contentName": content_name,
                }
            }
        },
    )

    await send_event(
        stream,
        {
            "event": {
                "promptEnd": {
                    "promptName": prompt_name,
                }
            }
        },
    )

    await send_event(
        stream,
        {
            "event": {
                "sessionEnd": {}
            }
        },
    )

    # Close only the input direction. The output receiver remains available
    # until receive_events() consumes completionEnd.
    await _close_input_stream_and_wait(stream)


async def receive_events(stream):
    """
    Receive Nova output events.

    Returns:
        {
            "audio": bytes,
            "text": str,
        }
    """

    audio_chunks = []
    assistant_text = ""
    content_roles = {}

    while True:
        output = await stream.await_output()
        result = await output[1].receive()

        if not result.value or not result.value.bytes_:
            continue

        response = json.loads(
            result.value.bytes_.decode("utf-8")
        )

        event = response.get("event", {})

        if "contentStart" in event:
            content_start = event["contentStart"]
            content_id = content_start.get("contentId")
            if content_id:
                content_roles[content_id] = content_start.get("role")

        elif "textOutput" in event:
            text = event["textOutput"].get(
                "content",
                "",
            )

            content_id = event["textOutput"].get("contentId")
            role = content_roles.get(content_id)
            if text and role == "ASSISTANT":
                assistant_text += text

        elif "audioOutput" in event:
            encoded_audio = event["audioOutput"].get(
                "content",
                "",
            )

            if encoded_audio:
                audio_chunks.append(
                    base64.b64decode(encoded_audio)
                )

        elif "completionEnd" in event:
            break

        elif "error" in event:
            raise RuntimeError(
                f"Nova error: {event['error']}"
            )

    return {
        "audio": b"".join(audio_chunks),
        "text": assistant_text,
    }


async def close_nova(client, stream):
    """Cleanly close the Nova stream and client."""

    cleanup_error = None

    try:
        await _close_input_stream_and_wait(stream)
    except Exception as exc:
        cleanup_error = exc

    try:
        output_stream = getattr(stream, "output_stream", None)
        if output_stream is not None:
            await output_stream.close()
    except Exception:
        pass

    try:
        await client.close()
    except Exception as exc:
        if cleanup_error is None:
            cleanup_error = exc

    if cleanup_error is not None:
        raise cleanup_error
async def send_audio_chunks_realtime(
    stream,
    prompt_name,
    content_name,
    audio_chunks,
):
    """
    Send already-buffered PCM audio chunks to Nova
    using the same pacing as the known-working
    nova_test.py implementation.
    """

    frame_duration_ms = 32

    for index, audio_chunk in enumerate(
        audio_chunks,
        start=1,
    ):

        encoded_audio = base64.b64encode(
            audio_chunk
        ).decode("utf-8")

        await send_event(
            stream,
            {
                "event": {
                    "audioInput": {
                        "promptName": prompt_name,
                        "contentName": content_name,
                        "content": encoded_audio,
                    }
                }
            },
        )

        if index <= 3 or index % 25 == 0:
            print(
                f"Nova audio chunk #{index}: "
                f"{len(audio_chunk)} bytes"
            )

        await asyncio.sleep(
            frame_duration_ms / 1000
        )
