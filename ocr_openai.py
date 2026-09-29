# -*- coding: utf-8 -*-
"""OpenAI OCR adapter; no scoring, UI, or provider fallback.

Like ocr_gemini.request_json, request_json returns JSON text. Use json.loads
or app.extract_json to obtain the existing Python dictionary structure.
"""

import base64
import json
import os
from copy import deepcopy

from openai import OpenAI

MODEL_NAME = os.getenv("OPENAI_MODEL", "gpt-5.6-sol")


def create_client(api_key=None):
    """Use an explicit key or OPENAI_API_KEY; never persist the key."""
    key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("OpenAI APIキーを引数またはOPENAI_API_KEYに設定してください。")
    return OpenAI(api_key=key.strip())


def _strict_schema(schema):
    """Copy existing schemas and close objects for OpenAI Structured Outputs.

    Field names, types, required lists, enums and nullability are preserved.
    The application's four schemas already require all declared fields.
    """
    result = deepcopy(schema)

    def visit(node):
        if not isinstance(node, dict):
            return
        if "properties" in node:
            properties = node["properties"]
            if set(node.get("required", [])) != set(properties):
                raise ValueError("Structured Outputs requires all properties in required.")
            if node.get("additionalProperties", False) is not False:
                raise ValueError("Structured Outputs requires additionalProperties=false.")
            node["additionalProperties"] = False
            for child in properties.values():
                visit(child)
        for keyword in ("items",):
            visit(node.get(keyword))
        for keyword in ("anyOf", "allOf", "oneOf"):
            for child in node.get(keyword, []):
                visit(child)
        for keyword in ("$defs", "definitions"):
            for child in node.get(keyword, {}).values():
                visit(child)

    visit(result)
    return result


def request_json(client, prompt, schema, images, identity_context=""):
    """Return JSON text for (name, image bytes, MIME type) images.

    Pass the existing prompt/schema and prepare_image_bytes output unchanged.
    Unreadable values remain null as instructed by the supplied prompt; this
    adapter does not fill, score, or otherwise alter extracted values.
    API errors propagate. Refusals/incomplete/empty responses raise ValueError.
    """
    content = [{"type": "input_text", "text": prompt}]
    if identity_context:
        content.append({"type": "input_text", "text": identity_context})
    image_count = 0
    for i, (name, data, mime) in enumerate(images, 1):
        if mime not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            raise ValueError(f"Unsupported image MIME type: {mime}")
        if not data:
            raise ValueError("Image data must not be empty.")
        encoded = base64.b64encode(data).decode("ascii")
        content.append({"type": "input_text", "text": f"【対象画像 {i}: {name}】"})
        content.append({
            "type": "input_image",
            "image_url": f"data:{mime};base64,{encoded}",
            "detail": "high",
        })
        image_count += 1
    if not image_count:
        raise ValueError("At least one image is required.")

    response = client.responses.create(
        model=MODEL_NAME,
        input=[{"role": "user", "content": content}],
        text={"format": {
            "type": "json_schema",
            "name": "ocr_extraction",
            "strict": True,
            "schema": _strict_schema(schema),
        }},
        store=False,
    )
    if response.status != "completed":
        raise ValueError(f"OpenAI OCR response was not completed: {response.status}")
    for item in response.output:
        for part in getattr(item, "content", []) or []:
            if getattr(part, "type", None) == "refusal":
                raise ValueError("OpenAI OCR request was refused.")
    text = response.output_text
    if not text or not text.strip():
        raise ValueError("OpenAI OCR returned no JSON text.")
    if not isinstance(json.loads(text), dict):
        raise ValueError("OpenAI OCR must return a JSON object.")
    return text
