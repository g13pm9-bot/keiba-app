# -*- coding: utf-8 -*-
"""Gemini-specific client creation and OCR requests."""

import os

from google import genai
from google.genai import types

MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
TEMPERATURE = 0.0
SEED = 7


def create_client(api_key):
    return genai.Client(api_key=api_key)


def image_part(data, mime):
    return types.Part.from_bytes(data=data, mime_type=mime)


def request_json(client, prompt, schema, images, identity_context=""):
    """Return JSON response text for (name, image bytes, MIME type) images."""
    contents = [prompt]
    if identity_context:
        contents.append(identity_context)
    for i, (name, data, mime) in enumerate(images, 1):
        contents.append(f"【対象画像 {i}: {name}】")
        contents.append(image_part(data, mime))

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=contents,
        config={
            "temperature": TEMPERATURE,
            "seed": SEED,
            "response_mime_type": "application/json",
            "response_json_schema": schema,
        },
    )
    return response.text
