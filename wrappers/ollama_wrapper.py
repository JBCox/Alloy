#!/usr/bin/env python3
"""Ollama wrapper for Alloy.

Calls local Ollama API to generate responses.
Requires: Ollama installed and running (https://ollama.ai)

Usage: python ollama_wrapper.py "your message here"
"""

import sys
import json
import urllib.request
import urllib.error

OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "llama3.1"  # Change to your preferred model


def query_ollama(message: str, model: str = DEFAULT_MODEL) -> str:
    """Query Ollama and return the response."""
    payload = {
        "model": model,
        "prompt": message,
        "stream": False
    }

    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={"Content-Type": "application/json"}
        )

        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result.get("response", "")

    except urllib.error.URLError as e:
        if "Connection refused" in str(e):
            return "Error: Ollama is not running. Start it with: ollama serve"
        return f"Error connecting to Ollama: {e}"
    except Exception as e:
        return f"Error: {e}"


def main():
    if len(sys.argv) < 2:
        print("Usage: python ollama_wrapper.py \"your message\"")
        print("       python ollama_wrapper.py \"your message\" model_name")
        sys.exit(1)

    message = sys.argv[1]
    model = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MODEL

    response = query_ollama(message, model)
    print(response)


if __name__ == "__main__":
    main()
