"""Gemini API 呼び出しをまとめたモジュール。"""

from collections.abc import Iterator

from google import genai
from google.genai import types

# gemini-2.5 系は新規ユーザー向けに提供終了（404 NOT_FOUND になる）
DEFAULT_MODEL = "gemini-3.8-flash"
MODEL_CHOICES = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
]


def stream_generate(
    api_key: str,
    model: str,
    prompt: str,
    system_instruction: str,
    temperature: float,
) -> Iterator[str]:
    """Gemini にプロンプトを送り、生成テキストを少しずつ返す。"""
    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=temperature,
    )
    for chunk in client.models.generate_content_stream(
        model=model, contents=prompt, config=config
    ):
        if chunk.text:
            yield chunk.text
