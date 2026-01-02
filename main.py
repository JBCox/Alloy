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

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory

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
        self.session = PromptSession(
            history=FileHistory(str(history_file)),
            auto_suggest=AutoSuggestFromHistory(),
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
        else:
            self.console.print(f"[yellow]Unknown command: /{cmd}[/yellow]")
        return True

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

    def query_single_ai(self, ai_name: str, message: str):
        color = self.get_ai_color(ai_name)
        with self.console.status(f"[{color}]Asking {ai_name}...[/{color}]"):
            response = self.orchestrator.query(ai_name, message)
        self.display_response(response)

    def query_all_ais(self, message: str):
        self.console.print("[dim]Querying all AIs...[/dim]\n")
        responses = self.orchestrator.query_all(message, parallel=self.config.all_parallel)
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

                color = self.get_ai_color(ai_name)
                with self.console.status(f"[{color}]{ai_name} is thinking...[/{color}]"):
                    response = self.orchestrator.query(ai_name, prompt)

                self.display_response(response)
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
            color = self.get_ai_color(ai_name)

            with self.console.status(f"[{color}]{ai_name} is refining...[/{color}]"):
                response = self.orchestrator.query(ai_name, prompt)

            self.display_response(response)
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
            with self.console.status(f"[dim]Consolidating ideas...[/dim]"):
                merge_response = self.orchestrator.query(judge, merge_prompt)

            self.display_response(merge_response)

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

        with self.console.status(f"[{self.get_ai_color(steelman_ai)}]{steelman_ai} building best case...[/]"):
            steelman_response = self.orchestrator.query(steelman_ai, prompt)
        self.display_response(steelman_response)
        self.console.print()

        if not steelman_response.success:
            return

        # Phase 2: Attack
        self.console.print("[dim]── Phase 2: Attack ──[/dim]\n")
        attack_ai = ais[1 % len(ais)]
        prompt = get_devils_advocate_prompt(topic, phase="attack", steelman=steelman_response.content)

        with self.console.status(f"[{self.get_ai_color(attack_ai)}]{attack_ai} finding weaknesses...[/]"):
            attack_response = self.orchestrator.query(attack_ai, prompt)
        self.display_response(attack_response)
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

        with self.console.status(f"[{self.get_ai_color(verdict_ai)}]{verdict_ai} synthesizing...[/]"):
            verdict_response = self.orchestrator.query(verdict_ai, prompt)
        self.display_response(verdict_response)

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

            with self.console.status(f"[{self.get_ai_color(ai_name)}]{ai_name} as {role_name}...[/]"):
                response = self.orchestrator.query(ai_name, prompt)

            self.display_response(response)
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

            with self.console.status(f"[{self.get_ai_color(ai_name)}]{ai_name} reviewing {review_type}...[/]"):
                response = self.orchestrator.query(ai_name, prompt)

            self.display_response(response)
            if response.success:
                all_reviews.append(f"=== {review_type.upper()} ({ai_name}) ===\n{response.content}")
            self.console.print()

        # Aggregate if we have reviews
        if all_reviews and len(all_reviews) > 1:
            self.console.print("[dim]── Aggregating Reviews ──[/dim]\n")
            aggregate_prompt = get_code_review_aggregate_prompt(code, "\n\n".join(all_reviews))

            judge = config.judge or self.config.default_ai
            with self.console.status(f"[dim]Consolidating feedback...[/dim]"):
                aggregate_response = self.orchestrator.query(judge, aggregate_prompt)
            self.display_response(aggregate_response)

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

            with self.console.status(f"[{self.get_ai_color(ai_name)}]{ai_name} working on {phase}...[/]"):
                response = self.orchestrator.query(ai_name, prompt)

            self.display_response(response)
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
