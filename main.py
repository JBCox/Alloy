#!/usr/bin/env python3
"""
Alloy - Multiple AIs, stronger together.

Usage:
    alloy

Commands:
    @claude <message>     - Send to Claude
    @gemini <message>     - Send to Gemini
    @codex <message>      - Send to Codex
    @all <message>        - Send to all AIs

Collaboration Modes:
    @roundtable <topic>   - Full-context discussion
    @chain <task>         - Sequential refinement
    @brainstorm <topic>   - Parallel idea generation
    @devils-advocate <idea> - Steelman/Attack/Verdict
    @roles[...] <task>    - Role-based collaboration
    @code-review <code>   - Multi-perspective review
    @solve <problem>      - Collaborative problem solving

System:
    /help    - Show help
    /ais     - List available AIs
    /quit    - Exit
"""

import sys
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add the script directory to path for imports
script_dir = Path(__file__).parent
sys.path.insert(0, str(script_dir))

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.live import Live
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion

from config import Config
from orchestrator import Orchestrator, AIResponse
from router import Router, CommandType, ParsedMessage
from modes import ModeType, ModeConfig, TerminationCondition, ROLE_TEMPLATES
from prompts import (
    get_roundtable_prompt, get_chain_prompt, get_brainstorm_prompt,
    get_devils_advocate_prompt, get_role_prompt, get_code_review_prompt,
    get_code_review_aggregate_prompt, get_solve_prompt,
    get_judge_quality_prompt, get_judge_consensus_prompt
)


class AlloyCompleter(Completer):
    """Autocomplete for Alloy commands and mentions."""

    # System commands
    SYSTEM_COMMANDS = [
        ("/help", "Show help"),
        ("/ais", "List available AIs"),
        ("/modes", "Show collaboration modes"),
        ("/settings", "Open settings editor"),
        ("/install", "AI installation assistant"),
        ("/pricing", "Compare AI pricing and tiers"),
        ("/gui", "Launch graphical interface"),
        ("/reload", "Reload configuration"),
        ("/history", "Show conversation history"),
        ("/clear", "Clear history"),
        ("/quit", "Exit Alloy"),
    ]

    # Collaboration modes with their options
    MODES = [
        ("@roundtable", "Full-context discussion"),
        ("@chain", "Sequential refinement"),
        ("@brainstorm", "Parallel idea generation"),
        ("@devils-advocate", "Steelman/Attack/Verdict"),
        ("@roles", "Role-based collaboration"),
        ("@code-review", "Multi-perspective review"),
        ("@solve", "Collaborative problem solving"),
        ("@all", "Query all AIs"),
    ]

    # Mode options (inside brackets)
    MODE_OPTIONS = [
        ("rounds=", "Number of rounds (e.g., rounds=3)"),
        ("order=", "AI order (e.g., order=claude,gemini)"),
        ("judge=", "Judge AI (e.g., judge=claude)"),
        ("template=", "Role template (architecture/debate/review/research)"),
        ("parallel", "Run in parallel"),
        ("checkpoint", "Pause after each round"),
    ]

    # Role templates
    ROLE_TEMPLATES = [
        ("architecture", "Architect, Critic, Implementer roles"),
        ("debate", "Proposer, Opponent, Moderator roles"),
        ("review", "Author, Reviewer, Editor roles"),
        ("research", "Researcher, Analyst, Synthesizer roles"),
    ]

    # Command flags
    FLAGS = [
        ("--checkpoint", "Pause after each round for review"),
        ("--until=satisfied", "Continue until quality threshold met"),
        ("--until=consensus", "Continue until AIs agree"),
        ("--implement", "Write code at the end"),
        ("--parallel", "Run in parallel mode"),
    ]

    def __init__(self, ai_names: list[str]):
        self.ai_names = ai_names

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        word = document.get_word_before_cursor(WORD=True)

        # Complete system commands starting with /
        if text.startswith("/") or word.startswith("/"):
            for cmd, desc in self.SYSTEM_COMMANDS:
                if cmd.startswith(word) or cmd.startswith(text):
                    yield Completion(
                        cmd,
                        start_position=-len(word),
                        display_meta=desc
                    )
            return

        # Complete flags starting with --
        if word.startswith("--") or (word.startswith("-") and len(word) >= 2):
            for flag, desc in self.FLAGS:
                if flag.startswith(word):
                    yield Completion(
                        flag,
                        start_position=-len(word),
                        display_meta=desc
                    )
            return

        # Check if we're inside brackets for mode options
        if "[" in text and "]" not in text.split("[")[-1]:
            # We're inside brackets - suggest mode options
            bracket_content = text.split("[")[-1]
            last_part = bracket_content.split(",")[-1].strip()

            # Check if we're completing a template= value
            if "template=" in last_part and "=" in last_part:
                template_prefix = last_part.split("=")[-1]
                for template, desc in self.ROLE_TEMPLATES:
                    if template.startswith(template_prefix):
                        yield Completion(
                            template,
                            start_position=-len(template_prefix),
                            display_meta=desc
                        )
                return

            # Check if we're completing a judge= or order= value (AI names)
            if ("judge=" in last_part or "order=" in last_part) and "=" in last_part:
                ai_prefix = last_part.split("=")[-1].split(",")[-1]
                for ai_name in self.ai_names:
                    if ai_name.startswith(ai_prefix):
                        yield Completion(
                            ai_name,
                            start_position=-len(ai_prefix),
                            display_meta=f"Use {ai_name}"
                        )
                return

            # Suggest mode options
            for opt, desc in self.MODE_OPTIONS:
                if opt.startswith(last_part):
                    yield Completion(
                        opt,
                        start_position=-len(last_part),
                        display_meta=desc
                    )
            return

        # Complete @ mentions and modes
        if text.startswith("@") or word.startswith("@"):
            # AI mentions
            for ai_name in self.ai_names:
                mention = f"@{ai_name}"
                if mention.startswith(word) or mention.startswith(text):
                    yield Completion(
                        mention,
                        start_position=-len(word),
                        display_meta=f"Send to {ai_name}"
                    )

            # Modes
            for mode, desc in self.MODES:
                if mode.startswith(word) or mode.startswith(text):
                    yield Completion(
                        mode,
                        start_position=-len(word),
                        display_meta=desc
                    )


# Color scheme for different AIs
AI_COLORS = {
    "claude": "cyan",
    "gemini": "blue",
    "codex": "green",
    "copilot": "magenta",
    "default": "white",
}


class ConversationHistory:
    """Manages conversation history."""

    def __init__(self, max_size: int = 50):
        self.messages: list[dict] = []
        self.max_size = max_size
        self.last_response: Optional[str] = None
        self.ai_responses: dict[str, str] = {}

    def add_user_message(self, content: str):
        self.messages.append({
            "role": "user",
            "content": content,
            "timestamp": datetime.now()
        })
        self._trim()

    def add_ai_response(self, ai_name: str, content: str):
        self.messages.append({
            "role": "assistant",
            "ai": ai_name,
            "content": content,
            "timestamp": datetime.now()
        })
        self.last_response = content
        self.ai_responses[ai_name.lower()] = content
        self._trim()

    def get_context(self) -> dict[str, str]:
        context = {
            "last": self.last_response or "",
            "history": self._format_history(),
        }
        context.update(self.ai_responses)
        return context

    def _format_history(self) -> str:
        lines = []
        for msg in self.messages[-10:]:
            if msg["role"] == "user":
                lines.append(f"User: {msg['content']}")
            else:
                lines.append(f"{msg['ai']}: {msg['content']}")
        return "\n".join(lines)

    def _trim(self):
        if len(self.messages) > self.max_size:
            self.messages = self.messages[-self.max_size:]

    def clear(self):
        self.messages = []
        self.last_response = None
        self.ai_responses = {}


class AICollab:
    """Main application class."""

    def __init__(self):
        self.console = Console()
        self.config = Config.load()
        self.orchestrator = Orchestrator(self.config)
        self.history = ConversationHistory(self.config.max_history)
        self.abort_requested = False

        available_ais = list(self.config.get_enabled_ais().keys())
        self.router = Router(available_ais, self.config.default_ai)

        history_file = Path.home() / ".alloy-history"
        self.completer = AlloyCompleter(available_ais)
        self.session = PromptSession(
            history=FileHistory(str(history_file)),
            auto_suggest=AutoSuggestFromHistory(),
            completer=self.completer,
            complete_while_typing=True,
        )

    def run(self):
        """Main loop."""
        self.print_welcome()

        while True:
            try:
                user_input = self.session.prompt("\n> ").strip()
                if not user_input:
                    continue

                parsed = self.router.parse(user_input)

                if parsed.command_type == CommandType.SYSTEM:
                    if not self.handle_system_command(parsed):
                        break
                else:
                    self.handle_ai_message(parsed, user_input)

            except KeyboardInterrupt:
                self.console.print("\n[dim]Interrupted. Use /quit to exit[/dim]")
                self.abort_requested = True
            except EOFError:
                break

        self.console.print("\n[dim]Goodbye![/dim]")

    def print_welcome(self):
        self.console.print()
        self.console.print(Panel.fit(
            "[bold]Alloy[/bold]\n"
            "Multiple AIs, stronger together\n\n"
            "[dim]Type /help for commands, /ais to see available AIs[/dim]",
            border_style="cyan"
        ))

        self.console.print("\n[dim]Checking AI availability...[/dim]")
        availability = self.orchestrator.check_availability()
        available = [n for n, a in availability.items() if a]
        unavailable = [n for n, a in availability.items() if not a]
        if available:
            self.console.print(f"[green]Available:[/green] {', '.join(available)}")
        if unavailable:
            self.console.print(f"[yellow]Not found:[/yellow] {', '.join(unavailable)}")

    def handle_system_command(self, parsed: ParsedMessage) -> bool:
        cmd = parsed.system_command
        if cmd in ("quit", "exit", "q"):
            return False
        elif cmd == "help":
            self.print_help()
        elif cmd == "history":
            self.print_history()
        elif cmd == "clear":
            self.history.clear()
            self.console.print("[dim]History cleared[/dim]")
        elif cmd in ("ais", "list"):
            self.print_ais()
        elif cmd == "modes":
            self.print_modes()
        elif cmd == "settings":
            self.open_settings()
        elif cmd == "reload":
            self._reload_config()
        elif cmd == "gui":
            self.launch_gui()
        elif cmd == "install":
            self.run_install_assistant()
        elif cmd == "pricing":
            self.show_pricing()
        else:
            self.console.print(f"[yellow]Unknown command: /{cmd}[/yellow]")
        return True

    def run_install_assistant(self):
        """Run the AI installation assistant."""
        try:
            from ai_installer import interactive_install
            interactive_install(self.config)
        except Exception as e:
            self.console.print(f"[red]Failed to run installer: {e}[/red]")

    def show_pricing(self):
        """Show AI pricing comparison."""
        try:
            from ai_installer import AIInstaller
            installer = AIInstaller(self.config)
            self.console.print(installer.get_pricing_info())
        except Exception as e:
            self.console.print(f"[red]Failed to get pricing: {e}[/red]")

    def launch_gui(self):
        """Launch the GUI application."""
        self.console.print("[dim]Launching GUI...[/dim]")
        try:
            from gui.app import run_gui
            # Run GUI in a separate process so CLI can continue or exit cleanly
            import subprocess
            import sys
            subprocess.Popen([sys.executable, "-c", "from gui.app import run_gui; run_gui()"])
            self.console.print("[green]GUI launched in a new window.[/green]")
        except Exception as e:
            self.console.print(f"[red]Failed to launch GUI: {e}[/red]")

    def _reload_config(self):
        """Reload configuration from disk."""
        try:
            self.config = Config.load()
            self.orchestrator = Orchestrator(self.config)
            self.console.print("[green]Configuration reloaded successfully.[/green]")
        except Exception as e:
            self.console.print(f"[red]Failed to reload config: {e}[/red]")

    def handle_ai_message(self, parsed: ParsedMessage, original: str):
        self.history.add_user_message(original)
        self.abort_requested = False

        context = self.history.get_context()
        content = self.router.substitute_variables(parsed.content, context)

        if parsed.command_type == CommandType.DIRECT:
            self.query_single_ai(parsed.target_ai, content)
        elif parsed.command_type == CommandType.DEFAULT:
            self.query_single_ai(self.config.default_ai, content)
        elif parsed.command_type == CommandType.ALL:
            self.query_all_ais(content)
        elif parsed.command_type == CommandType.MODE:
            self.execute_mode(parsed.mode_config, content)

    # ==================== BASIC QUERIES ====================

    def query_ai_with_display(self, ai_name: str, message: str, status_text: str = None) -> AIResponse:
        """Query an AI and display the response, using streaming if enabled."""
        color = self.get_ai_color(ai_name)
        status_text = status_text or f"Asking {ai_name}..."

        if self.config.streaming:
            stream = self.orchestrator.query_streaming(ai_name, message)
            return self.display_response_streaming(ai_name, stream)
        else:
            with self.console.status(f"[{color}]{status_text}[/{color}]"):
                response = self.orchestrator.query(ai_name, message)
            self.display_response(response)
            return response

    def query_single_ai(self, ai_name: str, message: str):
        if self.config.streaming:
            # Streaming mode - show output as it arrives
            stream = self.orchestrator.query_streaming(ai_name, message)
            self.display_response_streaming(ai_name, stream)
        else:
            # Blocking mode - wait for full response
            color = self.get_ai_color(ai_name)
            with self.console.status(f"[{color}]Asking {ai_name}...[/{color}]"):
                response = self.orchestrator.query(ai_name, message)
            self.display_response(response)

    def query_all_ais(self, message: str):
        self.console.print("[dim]Querying all AIs...[/dim]\n")
        use_parallel = self.config.parallel or self.config.all_parallel

        if self.config.streaming:
            # Streaming mode - show each AI's output as it arrives
            # For now, do sequential streaming (parallel streaming with multiple panels is more complex)
            for ai_name in self.config.get_enabled_ais().keys():
                if self.abort_requested:
                    break
                stream = self.orchestrator.query_streaming(ai_name, message)
                self.display_response_streaming(ai_name, stream)
                self.console.print()
        else:
            # Blocking mode - use existing parallel/sequential logic
            responses = self.orchestrator.query_all(message, parallel=use_parallel)
            for response in responses:
                self.display_response(response)
                self.console.print()

    # ==================== MODE EXECUTION ====================

    def execute_mode(self, config: ModeConfig, topic: str):
        """Execute a collaboration mode."""
        mode_handlers = {
            ModeType.ROUNDTABLE: self.run_roundtable,
            ModeType.CHAIN: self.run_chain,
            ModeType.BRAINSTORM: self.run_brainstorm,
            ModeType.DEVILS_ADVOCATE: self.run_devils_advocate,
            ModeType.ROLES: self.run_roles,
            ModeType.CODE_REVIEW: self.run_code_review,
            ModeType.SOLVE: self.run_solve,
        }
        handler = mode_handlers.get(config.mode_type)
        if handler:
            handler(config, topic)
        else:
            self.console.print(f"[red]Unknown mode: {config.mode_type}[/red]")

    def get_ai_order(self, config: ModeConfig) -> list[str]:
        """Get AI order for a mode, respecting custom order if set."""
        if config.order:
            return [ai for ai in config.order if ai in self.config.get_enabled_ais()]
        return list(self.config.get_enabled_ais().keys())

    def checkpoint_prompt(self, round_num: int, total: int, response_summary: str) -> str:
        """Show checkpoint and get user decision."""
        self.console.print(f"\n[dim]── Round {round_num}/{total} complete ──[/dim]")
        self.console.print(f"[dim]{response_summary[:200]}...[/dim]\n")

        try:
            choice = self.session.prompt(
                "[Continue/Accept/Modify/Abort] (c/a/m/q): "
            ).strip().lower()
        except (KeyboardInterrupt, EOFError):
            return "abort"

        if choice in ("a", "accept"):
            return "accept"
        elif choice in ("m", "modify"):
            return "modify"
        elif choice in ("q", "abort"):
            return "abort"
        return "continue"

    def check_quality(self, config: ModeConfig, topic: str, response: str) -> tuple[int, bool]:
        """Check response quality using judge AI. Returns (score, should_continue)."""
        judge = config.judge or self.config.default_ai
        prompt = get_judge_quality_prompt(topic, response)

        with self.console.status(f"[dim]Judging quality...[/dim]"):
            result = self.orchestrator.query(judge, prompt)

        if not result.success:
            return 5, True  # Default to continue on error

        # Parse rating from response
        match = re.search(r"Rating:\s*(\d+)/10", result.content)
        score = int(match.group(1)) if match else 5

        self.console.print(f"[dim]Quality: {score}/10[/dim]")
        return score, score < config.satisfaction_threshold

    # ==================== ROUNDTABLE ====================

    def run_roundtable(self, config: ModeConfig, topic: str):
        """Full-context roundtable discussion."""
        ais = self.get_ai_order(config)
        if len(ais) < 2:
            self.console.print("[yellow]Need at least 2 AIs for roundtable[/yellow]")
            return

        self.console.print(f"[bold]Roundtable:[/bold] {topic}\n")
        all_responses = []

        for round_num in range(1, config.rounds + 1):
            if self.abort_requested:
                break

            self.console.print(f"[dim]── Round {round_num} ──[/dim]\n")

            for i, ai_name in enumerate(ais):
                if self.abort_requested:
                    break

                # Build context with ALL previous responses
                context = "\n\n".join([
                    f"{r['ai']}: {r['content']}" for r in all_responses
                ]) if all_responses else ""

                prompt = get_roundtable_prompt(topic, context, is_first=(len(all_responses) == 0))

                response = self.query_ai_with_display(ai_name, prompt, f"{ai_name} is thinking...")
                if response.success:
                    all_responses.append({"ai": ai_name, "content": response.content})
                self.console.print()

            if config.checkpoint and round_num < config.rounds:
                action = self.checkpoint_prompt(round_num, config.rounds, all_responses[-1]["content"] if all_responses else "")
                if action == "abort":
                    break
                elif action == "accept":
                    break

        self.console.print("[dim]── Roundtable complete ──[/dim]")

    # ==================== CHAIN ====================

    def run_chain(self, config: ModeConfig, topic: str):
        """Sequential refinement chain."""
        ais = self.get_ai_order(config)
        if not ais:
            self.console.print("[yellow]No AIs available[/yellow]")
            return

        self.console.print(f"[bold]Chain:[/bold] {topic}\n")
        current_response = None
        round_num = 0

        while round_num < config.rounds and round_num < config.max_rounds:
            if self.abort_requested:
                break

            ai_name = ais[round_num % len(ais)]
            round_num += 1

            self.console.print(f"[dim]── Round {round_num}/{config.rounds} ({ai_name}) ──[/dim]\n")

            prompt = get_chain_prompt(topic, current_response)
            response = self.query_ai_with_display(ai_name, prompt, f"{ai_name} is refining...")
            self.console.print()

            if response.success:
                current_response = response.content

            # Check quality if --until=satisfied
            if config.until == TerminationCondition.SATISFIED and response.success:
                score, should_continue = self.check_quality(config, topic, current_response)
                if not should_continue:
                    self.console.print(f"[green]Quality threshold reached ({score}/10)[/green]")
                    break

            # Checkpoint
            if config.checkpoint and round_num < config.rounds:
                action = self.checkpoint_prompt(round_num, config.rounds, current_response or "")
                if action == "abort":
                    break
                elif action == "accept":
                    break

        self.console.print("[dim]── Chain complete ──[/dim]")

    # ==================== BRAINSTORM ====================

    def run_brainstorm(self, config: ModeConfig, topic: str):
        """Parallel brainstorming with merge."""
        ais = self.get_ai_order(config)
        if not ais:
            self.console.print("[yellow]No AIs available[/yellow]")
            return

        self.console.print(f"[bold]Brainstorm:[/bold] {topic}\n")

        # Phase 1: Generate ideas (parallel or sequential)
        self.console.print("[dim]── Generating ideas ──[/dim]\n")
        prompt = get_brainstorm_prompt(topic, phase="generate")

        if config.parallel:
            responses = self.orchestrator.query_all(prompt, parallel=True)
        else:
            responses = [self.orchestrator.query(ai, prompt) for ai in ais]

        all_ideas = []
        for response in responses:
            self.display_response(response)
            if response.success:
                all_ideas.append(f"From {response.ai_name}:\n{response.content}")
            self.console.print()

        # Phase 2: Merge and refine
        if all_ideas:
            self.console.print("[dim]── Merging ideas ──[/dim]\n")
            merge_prompt = get_brainstorm_prompt(topic, phase="merge", all_ideas="\n\n".join(all_ideas))

            judge = config.judge or self.config.default_ai
            self.query_ai_with_display(judge, merge_prompt, "Consolidating ideas...")

        self.console.print("\n[dim]── Brainstorm complete ──[/dim]")

    # ==================== DEVIL'S ADVOCATE ====================

    def run_devils_advocate(self, config: ModeConfig, topic: str):
        """Steelman -> Attack -> Verdict analysis."""
        ais = self.get_ai_order(config)
        if len(ais) < 2:
            self.console.print("[yellow]Need at least 2 AIs for devil's advocate[/yellow]")
            return

        self.console.print(f"[bold]Devil's Advocate:[/bold] {topic}\n")

        # Phase 1: Steelman
        self.console.print("[dim]── Phase 1: Steelman ──[/dim]\n")
        steelman_ai = ais[0]
        prompt = get_devils_advocate_prompt(topic, phase="steelman")
        steelman_response = self.query_ai_with_display(steelman_ai, prompt, f"{steelman_ai} building best case...")
        self.console.print()

        if not steelman_response.success:
            return

        # Phase 2: Attack
        self.console.print("[dim]── Phase 2: Attack ──[/dim]\n")
        attack_ai = ais[1 % len(ais)]
        prompt = get_devils_advocate_prompt(topic, phase="attack", steelman=steelman_response.content)
        attack_response = self.query_ai_with_display(attack_ai, prompt, f"{attack_ai} finding weaknesses...")
        self.console.print()

        if not attack_response.success:
            return

        # Phase 3: Verdict
        self.console.print("[dim]── Phase 3: Verdict ──[/dim]\n")
        verdict_ai = ais[2 % len(ais)] if len(ais) >= 3 else config.judge or self.config.default_ai
        prompt = get_devils_advocate_prompt(
            topic, phase="verdict",
            steelman=steelman_response.content,
            attack=attack_response.content
        )
        self.query_ai_with_display(verdict_ai, prompt, f"{verdict_ai} synthesizing...")

        self.console.print("\n[dim]── Devil's Advocate complete ──[/dim]")

    # ==================== ROLES ====================

    def run_roles(self, config: ModeConfig, topic: str):
        """Role-based collaboration."""
        roles = config.get_effective_roles()
        ais = self.get_ai_order(config)

        if config.template and config.template in ROLE_TEMPLATES:
            template = ROLE_TEMPLATES[config.template]
            role_names = list(template.keys())
        elif roles:
            role_names = list(roles.keys())
        else:
            self.console.print("[yellow]No roles defined. Use [template=...] or [role=ai][/yellow]")
            return

        self.console.print(f"[bold]Roles ({config.template or 'custom'}):[/bold] {topic}\n")

        all_contributions = []

        for i, role_name in enumerate(role_names):
            if self.abort_requested:
                break

            # Determine which AI plays this role
            ai_name = roles.get(role_name) or ais[i % len(ais)]
            role_desc = ""
            if config.template and config.template in ROLE_TEMPLATES:
                role_desc = ROLE_TEMPLATES[config.template].get(role_name, "")

            self.console.print(f"[dim]── {role_name.title()} ({ai_name}) ──[/dim]\n")

            context = "\n\n".join([
                f"{c['role']}: {c['content']}" for c in all_contributions
            ]) if all_contributions else ""

            prompt = get_role_prompt(role_name, role_desc, topic, context)
            response = self.query_ai_with_display(ai_name, prompt, f"{ai_name} as {role_name}...")
            if response.success:
                all_contributions.append({"role": role_name, "content": response.content})
            self.console.print()

        self.console.print("[dim]── Roles complete ──[/dim]")

    # ==================== CODE REVIEW ====================

    def run_code_review(self, config: ModeConfig, code: str):
        """Multi-perspective code review."""
        ais = self.get_ai_order(config)
        if not ais:
            self.console.print("[yellow]No AIs available[/yellow]")
            return

        self.console.print(f"[bold]Code Review[/bold]\n")

        review_types = ["security", "performance", "quality"]
        all_reviews = []

        # Assign review types to AIs
        for i, review_type in enumerate(review_types):
            if self.abort_requested:
                break

            ai_name = ais[i % len(ais)]
            self.console.print(f"[dim]── {review_type.title()} Review ({ai_name}) ──[/dim]\n")

            prompt = get_code_review_prompt(code, review_type)
            response = self.query_ai_with_display(ai_name, prompt, f"{ai_name} reviewing {review_type}...")
            if response.success:
                all_reviews.append(f"=== {review_type.upper()} ({ai_name}) ===\n{response.content}")
            self.console.print()

        # Aggregate if we have reviews
        if all_reviews and len(all_reviews) > 1:
            self.console.print("[dim]── Aggregating Reviews ──[/dim]\n")
            aggregate_prompt = get_code_review_aggregate_prompt(code, "\n\n".join(all_reviews))

            judge = config.judge or self.config.default_ai
            self.query_ai_with_display(judge, aggregate_prompt, "Consolidating feedback...")

        self.console.print("\n[dim]── Code Review complete ──[/dim]")

    # ==================== SOLVE ====================

    def run_solve(self, config: ModeConfig, problem: str):
        """Collaborative problem solving."""
        ais = self.get_ai_order(config)
        if not ais:
            self.console.print("[yellow]No AIs available[/yellow]")
            return

        self.console.print(f"[bold]Solve:[/bold] {problem}\n")

        phases = ["understand", "plan", "implement"]
        understanding = ""
        plan = ""

        for i, phase in enumerate(phases):
            if self.abort_requested:
                break

            ai_name = ais[i % len(ais)]
            self.console.print(f"[dim]── Phase {i+1}: {phase.title()} ({ai_name}) ──[/dim]\n")

            prompt = get_solve_prompt(problem, phase, understanding=understanding, plan=plan)
            response = self.query_ai_with_display(ai_name, prompt, f"{ai_name} working on {phase}...")
            self.console.print()

            if response.success:
                if phase == "understand":
                    understanding = response.content
                elif phase == "plan":
                    plan = response.content

            # Checkpoint between phases
            if config.checkpoint and i < len(phases) - 1:
                action = self.checkpoint_prompt(i + 1, len(phases), response.content if response.success else "")
                if action == "abort":
                    break
                elif action == "accept":
                    break

        self.console.print("[dim]── Solve complete ──[/dim]")

    # ==================== DISPLAY ====================

    def display_response_streaming(self, ai_name: str, stream_generator) -> AIResponse:
        """Display a streaming response with live updates."""
        color = self.get_ai_color(ai_name)
        content_buffer = ""
        error = None

        def make_panel(text: str, streaming: bool = True) -> Panel:
            """Create a panel for the current content."""
            try:
                display_content = Markdown(text) if text.strip() else Text("...")
            except Exception:
                display_content = Text(text)

            suffix = " [dim]streaming...[/dim]" if streaming else ""
            return Panel(
                display_content,
                title=f"[{color} bold]{ai_name}[/{color} bold]{suffix}",
                border_style=color,
                padding=(0, 1)
            )

        try:
            with Live(make_panel(""), refresh_per_second=self.config.refresh_rate, console=self.console) as live:
                for chunk in stream_generator:
                    if isinstance(chunk, str):
                        content_buffer += chunk
                        # Update display periodically (every few chars to reduce flicker)
                        if len(content_buffer) % 10 == 0 or chunk == '\n':
                            live.update(make_panel(content_buffer, streaming=True))

                # Final update without streaming indicator
                live.update(make_panel(content_buffer, streaming=False))

        except KeyboardInterrupt:
            self.abort_requested = True
            error = "Interrupted by user"

        # Build the response
        response = AIResponse(
            ai_name=ai_name,
            content=content_buffer.strip(),
            success=error is None,
            error=error
        )

        if response.success:
            self.history.add_ai_response(ai_name, response.content)

        return response

    def display_response(self, response: AIResponse):
        color = self.get_ai_color(response.ai_name)

        if not response.success:
            self.console.print(Panel(
                f"[red]{response.error}[/red]",
                title=f"[{color}]{response.ai_name}[/{color}] [red]Error[/red]",
                border_style="red"
            ))
            return

        self.history.add_ai_response(response.ai_name, response.content)

        try:
            content = Markdown(response.content)
        except Exception:
            content = response.content

        if self.config.show_ai_header:
            self.console.print(Panel(
                content,
                title=f"[{color} bold]{response.ai_name}[/{color} bold]",
                border_style=color,
                padding=(0, 1)
            ))
        else:
            self.console.print(content)

    def get_ai_color(self, ai_name: str) -> str:
        ai_config = self.config.get_ai(ai_name)
        if ai_config:
            return ai_config.color
        return AI_COLORS.get(ai_name.lower(), AI_COLORS["default"])

    def print_help(self):
        help_text = """
[bold]Alloy Commands[/bold]

[cyan]Direct Messaging:[/cyan]
  @<ai> <message>          Send to specific AI
  @all <message>           Send to all AIs
  <message>                Send to default AI

[cyan]Collaboration Modes:[/cyan]
  @roundtable <topic>      Full-context discussion
  @chain <task>            Sequential refinement
  @brainstorm <topic>      Parallel idea generation
  @devils-advocate <idea>  Steelman/Attack/Verdict
  @roles[...] <task>       Role-based collaboration
  @code-review <code>      Multi-perspective review
  @solve <problem>         Collaborative problem solving

[cyan]Mode Options:[/cyan]
  @chain[rounds=5,judge=claude] --checkpoint <task>
  @roles[template=architecture] <task>
  @roles[architect=claude,critic=gemini] <task>

[cyan]Options:[/cyan]
  rounds=N                 Number of rounds
  order=ai1,ai2,ai3        Custom AI order
  judge=<ai>               AI to judge quality
  template=<name>          Role template (architecture/debate/review/research)

[cyan]Flags:[/cyan]
  --checkpoint             Pause after each round
  --until=satisfied        Continue until quality threshold
  --implement              Write code at end

[cyan]Variables:[/cyan]
  {last}                   Last AI response
  {claude}, {gemini}       Specific AI's last response

[cyan]System:[/cyan]
  /help                    Show this help
  /ais                     List available AIs
  /modes                   Show collaboration modes
  /settings                Open settings editor
  /install                 AI installation assistant
  /pricing                 Compare AI pricing and tiers
  /gui                     Launch graphical interface
  /reload                  Reload configuration
  /history                 Show conversation
  /clear                   Clear history
  /quit                    Exit
"""
        self.console.print(help_text)

    def print_history(self):
        if not self.history.messages:
            self.console.print("[dim]No history yet[/dim]")
            return
        for msg in self.history.messages:
            timestamp = msg["timestamp"].strftime("%H:%M:%S")
            if msg["role"] == "user":
                self.console.print(f"[dim]{timestamp}[/dim] [bold]You:[/bold] {msg['content'][:100]}...")
            else:
                ai = msg["ai"]
                color = self.get_ai_color(ai)
                preview = msg["content"][:100].replace("\n", " ")
                self.console.print(f"[dim]{timestamp}[/dim] [{color}]{ai}:[/{color}] {preview}...")

    def print_ais(self):
        table = Table(title="Available AIs")
        table.add_column("Name", style="cyan")
        table.add_column("Status")
        table.add_column("Description")

        availability = self.orchestrator.check_availability()
        for name, ai in self.config.get_enabled_ais().items():
            status = "[green]Ready[/green]" if availability.get(name) else "[red]Not found[/red]"
            is_default = " [dim](default)[/dim]" if name == self.config.default_ai else ""
            table.add_row(f"@{name}{is_default}", status, ai.description)

        self.console.print(table)

    def open_settings(self):
        """Open settings editor in separate process."""
        import multiprocessing

        def run_settings_editor(config_path_str):
            import tkinter as tk
            from gui.settings import SettingsEditor
            from pathlib import Path

            root = tk.Tk()
            root.withdraw()
            editor = SettingsEditor(root, config_path=Path(config_path_str))
            root.mainloop()

        config_path = Config.get_config_path()

        # Launch in separate process so CLI is not blocked
        p = multiprocessing.Process(
            target=run_settings_editor,
            args=(str(config_path),)
        )
        p.start()

        self.console.print("[dim]Settings editor opened in new window.[/dim]")
        self.console.print("[dim]Changes will take effect after restarting Alloy.[/dim]")

    def print_modes(self):
        table = Table(title="Collaboration Modes")
        table.add_column("Mode", style="cyan")
        table.add_column("Description")
        table.add_column("Default Rounds")

        modes = [
            ("@roundtable", "Full-context discussion", "1 per AI"),
            ("@chain", "Sequential refinement", "3"),
            ("@brainstorm", "Parallel idea generation", "1"),
            ("@devils-advocate", "Steelman/Attack/Verdict", "3 phases"),
            ("@roles", "Role-based collaboration", "1 per role"),
            ("@code-review", "Multi-perspective review", "3 reviews"),
            ("@solve", "Collaborative problem solving", "3 phases"),
        ]
        for mode, desc, rounds in modes:
            table.add_row(mode, desc, rounds)

        self.console.print(table)
        self.console.print("\n[dim]Use /help for syntax and options[/dim]")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Alloy - Multiple AIs, stronger together",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py              Start CLI mode
  python main.py --gui        Start GUI mode
  python main.py --setup      Run setup wizard
"""
    )
    parser.add_argument("--gui", action="store_true", help="Launch graphical interface")
    parser.add_argument("--setup", action="store_true", help="Run setup wizard")

    args = parser.parse_args()

    # Handle --gui flag
    if args.gui:
        try:
            from gui.app import run_gui
            run_gui()
            return
        except ImportError as e:
            Console().print(f"[red]GUI not available: {e}[/red]")
            sys.exit(1)

    # Handle --setup flag
    if args.setup:
        try:
            import tkinter as tk
            from gui.wizard import SetupWizard

            root = tk.Tk()
            root.withdraw()

            def on_complete():
                root.destroy()

            wizard = SetupWizard(root, on_complete=on_complete)
            root.mainloop()
            return
        except ImportError as e:
            Console().print(f"[red]Setup wizard not available: {e}[/red]")
            sys.exit(1)

    # Check for first run - launch setup wizard if no config exists
    if not Config.config_exists():
        try:
            import tkinter as tk
            from gui.wizard import SetupWizard

            console = Console()
            console.print("\n[cyan]Welcome to Alloy![/cyan]")
            console.print("[dim]No configuration found. Launching setup wizard...[/dim]\n")

            # Run the setup wizard
            root = tk.Tk()
            root.withdraw()

            wizard_completed = [False]  # Use list to allow mutation in closure

            def on_complete():
                wizard_completed[0] = True
                root.destroy()

            wizard = SetupWizard(root, on_complete=on_complete)
            root.mainloop()

            if not wizard_completed[0]:
                console.print("[yellow]Setup cancelled. Run 'alloy' again to restart setup.[/yellow]")
                sys.exit(0)

            console.print("[green]Setup complete! Starting Alloy...[/green]\n")

        except ImportError as e:
            Console().print(f"[yellow]GUI not available ({e}). Using default configuration.[/yellow]")
        except Exception as e:
            Console().print(f"[yellow]Setup wizard failed: {e}. Using default configuration.[/yellow]")

    try:
        app = AICollab()
        app.run()
    except Exception as e:
        Console().print(f"[red]Error: {e}[/red]")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
