#!/usr/bin/env python3
"""Groq wrapper for Alloy.

Calls Groq API for fast LLM inference.
Requires: GROQ_API_KEY environment variable

Free tier: https://console.groq.com (generous limits!)

Usage: python groq_wrapper.py "your message here"
"""

import sys
import os
import json
import urllib.request
import urllib.error

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.1-70b-versatile"  # Fast and capable


def query_groq(message: str, model: str = DEFAULT_MODEL) -> str:
    """Query Groq API and return the response."""
    api_key = os.environ.get("GROQ_API_KEY")

    if not api_key:
        return "Error: GROQ_API_KEY not set. Get a free key at https://console.groq.com"

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": message}],
        "temperature": 0.7,
        "max_tokens": 4096
    }

    try:
        req = urllib.request.Request(
            GROQ_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }
        )

        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result["choices"][0]["message"]["content"]

    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        if e.code == 401:
            return "Error: Invalid GROQ_API_KEY. Check your key at https://console.groq.com"
        elif e.code == 429:
            return "Error: Rate limit exceeded. Wait a moment and try again."
        return f"Error {e.code}: {error_body}"
    except Exception as e:
        return f"Error: {e}"


def main():
    if len(sys.argv) < 2:
        print("Usage: python groq_wrapper.py \"your message\"")
        print("       python groq_wrapper.py \"your message\" model_name")
        print("\nModels: llama-3.1-70b-versatile, llama-3.1-8b-instant, mixtral-8x7b-32768")
        sys.exit(1)

    message = sys.argv[1]
    model = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MODEL

    response = query_groq(message, model)
    print(response)


if __name__ == "__main__":
    main()
