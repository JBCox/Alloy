"""AI subprocess management for AI Collab."""

import subprocess
import shlex
import sys
import os
from dataclasses import dataclass
from typing import Optional, Generator
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import AIConfig, Config


@dataclass
class AIResponse:
    """Response from an AI CLI."""
    ai_name: str
    content: str
    success: bool
    error: Optional[str] = None


class Orchestrator:
    """Manages AI CLI subprocesses."""

    def __init__(self, config: Config):
        self.config = config
        self.timeout = 300  # 5 minute timeout for AI responses

    def query(self, ai_name: str, message: str) -> AIResponse:
        """
        Send a query to a specific AI and get the response.

        Args:
            ai_name: Name of the AI to query (e.g., "claude", "gemini")
            message: The message/prompt to send

        Returns:
            AIResponse with the result
        """
        ai_config = self.config.get_ai(ai_name)
        if ai_config is None:
            return AIResponse(
                ai_name=ai_name,
                content="",
                success=False,
                error=f"Unknown or disabled AI: {ai_name}"
            )

        return self._execute(ai_config, message)

    def query_streaming(self, ai_name: str, message: str) -> Generator[str, None, AIResponse]:
        """
        Stream a query to a specific AI, yielding chunks as they arrive.

        Args:
            ai_name: Name of the AI to query
            message: The message/prompt to send

        Yields:
            String chunks as they arrive from the AI

        Returns:
            AIResponse with the complete result (via generator return)
        """
        ai_config = self.config.get_ai(ai_name)
        if ai_config is None:
            return AIResponse(
                ai_name=ai_name,
                content="",
                success=False,
                error=f"Unknown or disabled AI: {ai_name}"
            )

        return self._execute_streaming(ai_config, message)

    def _execute_streaming(self, ai_config: AIConfig, message: str) -> Generator[str, None, AIResponse]:
        """Execute an AI CLI command with streaming output."""
        full_content = ""
        error = None

        try:
            escaped_message = self._escape_message(message)
            command = ai_config.command.replace("{message}", escaped_message)

            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, "NO_COLOR": "1", "PYTHONUNBUFFERED": "1"},
            )

            # Stream stdout character by character for responsive output
            while True:
                char = process.stdout.read(1)
                if not char:
                    break
                full_content += char
                yield char

            process.wait(timeout=self.timeout)

            if process.returncode != 0:
                stderr = process.stderr.read().strip()
                if stderr and not full_content:
                    error = f"Command failed: {stderr}"

        except subprocess.TimeoutExpired:
            process.kill()
            error = f"Timeout: AI did not respond within {self.timeout} seconds"
        except FileNotFoundError:
            error = f"CLI not found: {ai_config.command.split()[0]}. Is it installed and in PATH?"
        except Exception as e:
            error = f"Error: {str(e)}"

        return AIResponse(
            ai_name=ai_config.name,
            content=full_content.strip(),
            success=error is None,
            error=error
        )

    def query_all(self, message: str, parallel: bool = False) -> list[AIResponse]:
        """
        Send a query to all enabled AIs.

        Args:
            message: The message/prompt to send
            parallel: Whether to run queries in parallel

        Returns:
            List of AIResponse objects
        """
        enabled_ais = self.config.get_enabled_ais()

        if parallel:
            return self._query_parallel(list(enabled_ais.values()), message)
        else:
            return self._query_sequential(list(enabled_ais.values()), message)

    def _query_sequential(self, ais: list[AIConfig], message: str) -> list[AIResponse]:
        """Query AIs one by one."""
        responses = []
        for ai in ais:
            responses.append(self._execute(ai, message))
        return responses

    def _query_parallel(self, ais: list[AIConfig], message: str) -> list[AIResponse]:
        """Query all AIs in parallel."""
        responses = []
        with ThreadPoolExecutor(max_workers=len(ais)) as executor:
            future_to_ai = {
                executor.submit(self._execute, ai, message): ai
                for ai in ais
            }
            for future in as_completed(future_to_ai):
                responses.append(future.result())
        return responses

    def _execute(self, ai_config: AIConfig, message: str) -> AIResponse:
        """Execute an AI CLI command and capture output."""
        try:
            # Build the command
            # Escape the message for shell
            escaped_message = self._escape_message(message)
            command = ai_config.command.replace("{message}", escaped_message)

            # Determine shell based on platform
            if sys.platform == "win32":
                # On Windows, run through cmd or powershell
                shell = True
            else:
                shell = True

            # Run the command
            result = subprocess.run(
                command,
                shell=shell,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, "NO_COLOR": "1"},  # Disable colors for clean output
            )

            if result.returncode != 0:
                # Check if there's useful output despite non-zero return
                output = result.stdout.strip() or result.stderr.strip()
                if output:
                    return AIResponse(
                        ai_name=ai_config.name,
                        content=output,
                        success=True  # Consider it success if we got output
                    )
                return AIResponse(
                    ai_name=ai_config.name,
                    content="",
                    success=False,
                    error=f"Command failed: {result.stderr.strip()}"
                )

            return AIResponse(
                ai_name=ai_config.name,
                content=result.stdout.strip(),
                success=True
            )

        except subprocess.TimeoutExpired:
            return AIResponse(
                ai_name=ai_config.name,
                content="",
                success=False,
                error=f"Timeout: AI did not respond within {self.timeout} seconds"
            )
        except FileNotFoundError:
            return AIResponse(
                ai_name=ai_config.name,
                content="",
                success=False,
                error=f"CLI not found: {ai_config.command.split()[0]}. Is it installed and in PATH?"
            )
        except Exception as e:
            return AIResponse(
                ai_name=ai_config.name,
                content="",
                success=False,
                error=f"Error: {str(e)}"
            )

    def _escape_message(self, message: str) -> str:
        """Escape message for shell command."""
        # Use double quotes and escape internal quotes
        if sys.platform == "win32":
            # Windows escaping
            escaped = message.replace('"', '\\"')
            return f'"{escaped}"'
        else:
            # Unix escaping
            return shlex.quote(message)

    def check_availability(self) -> dict[str, bool]:
        """Check which AIs are available (CLI installed and working)."""
        availability = {}
        for name, ai in self.config.get_enabled_ais().items():
            # Try to run a simple command to check if CLI exists
            cmd_parts = ai.command.split()[0]  # Get just the executable
            try:
                result = subprocess.run(
                    [cmd_parts, "--version"],
                    capture_output=True,
                    timeout=10,
                    shell=sys.platform == "win32"
                )
                availability[name] = True
            except (subprocess.SubprocessError, FileNotFoundError):
                # Try --help as fallback
                try:
                    result = subprocess.run(
                        [cmd_parts, "--help"],
                        capture_output=True,
                        timeout=10,
                        shell=sys.platform == "win32"
                    )
                    availability[name] = True
                except (subprocess.SubprocessError, FileNotFoundError):
                    availability[name] = False
        return availability
