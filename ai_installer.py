"""AI Installation Assistant.

Uses a working AI to help install and configure missing AIs.
"""

import subprocess
import platform
import os
import webbrowser
from dataclasses import dataclass
from typing import Optional, Callable
from pathlib import Path

from config import Config
from orchestrator import Orchestrator


@dataclass
class AIPricing:
    """Pricing information for an AI."""
    has_free_tier: bool
    free_tier_limits: str  # Description of free tier limits
    paid_price: str  # Price description
    price_per_request: str  # Approximate cost per request
    best_for: str  # Who this tier is best for


@dataclass
class AIInstallInfo:
    """Installation information for an AI."""
    name: str
    description: str
    install_type: str  # "api_key", "cli_tool", "both"
    api_key_env: Optional[str]  # Environment variable name
    api_key_url: Optional[str]  # URL to get API key
    cli_command: Optional[str]  # Command to check if installed
    install_instructions: str  # Basic instructions
    detailed_help: str  # Detailed help for AI to provide
    pricing: Optional[AIPricing] = None  # Pricing information


# Installation info for each AI
AI_INSTALL_INFO = {
    "claude": AIInstallInfo(
        name="Claude (Anthropic)",
        description="Anthropic's Claude AI - excellent for analysis and coding",
        install_type="api_key",
        api_key_env="ANTHROPIC_API_KEY",
        api_key_url="https://console.anthropic.com/settings/keys",
        cli_command=None,
        install_instructions="""
1. Go to https://console.anthropic.com/
2. Sign up or log in
3. Go to Settings > API Keys
4. Create a new API key
5. Set ANTHROPIC_API_KEY environment variable
""",
        detailed_help="""
Claude is accessed via the Anthropic API. Users need to:
1. Create an Anthropic account at console.anthropic.com
2. Add billing information (pay-as-you-go)
3. Generate an API key from Settings > API Keys
4. Set the ANTHROPIC_API_KEY environment variable

On Windows: setx ANTHROPIC_API_KEY "sk-ant-..."
On Mac/Linux: export ANTHROPIC_API_KEY="sk-ant-..."

The API key starts with "sk-ant-". Typical costs are $0.01-0.02 per request.
""",
        pricing=AIPricing(
            has_free_tier=False,
            free_tier_limits="CLI uses API credits - check console.anthropic.com for current pricing",
            paid_price="Pay-as-you-go: ~$3/M input, ~$15/M output (Sonnet)",
            price_per_request="Varies by model and usage",
            best_for="Coding tasks, complex analysis - check Anthropic for current offers"
        )
    ),

    "gemini": AIInstallInfo(
        name="Gemini (Google)",
        description="Google's Gemini AI - great for research and creative tasks",
        install_type="api_key",
        api_key_env="GOOGLE_API_KEY",
        api_key_url="https://aistudio.google.com/app/apikey",
        cli_command=None,
        install_instructions="""
1. Go to https://aistudio.google.com/
2. Sign in with Google account
3. Click "Get API Key"
4. Create a new API key
5. Set GOOGLE_API_KEY environment variable
""",
        detailed_help="""
Gemini is accessed via the Google AI Studio API. Users need to:
1. Go to aistudio.google.com
2. Sign in with a Google account
3. Click "Get API Key" in the left sidebar
4. Create a key for a new or existing project
5. Set the GOOGLE_API_KEY environment variable

On Windows: setx GOOGLE_API_KEY "AIza..."
On Mac/Linux: export GOOGLE_API_KEY="AIza..."

The API key starts with "AIza". Google offers a free tier with generous limits.
""",
        pricing=AIPricing(
            has_free_tier=True,
            free_tier_limits="15 requests/minute, 1M tokens/month FREE (Gemini 1.5 Flash)",
            paid_price="$0.075/M input, $0.30/M output (Flash) - very affordable",
            price_per_request="~$0.001-0.01 per request (often FREE)",
            best_for="Budget-conscious users, high-volume usage, students"
        )
    ),

    "copilot": AIInstallInfo(
        name="GitHub Copilot",
        description="GitHub's AI - specialized for code completion",
        install_type="cli_tool",
        api_key_env=None,
        api_key_url="https://github.com/features/copilot",
        cli_command="gh copilot --version",
        install_instructions="""
1. Install GitHub CLI: https://cli.github.com/
2. Run: gh auth login
3. Run: gh extension install github/gh-copilot
4. Requires GitHub Copilot subscription ($10/month or free for students)
""",
        detailed_help="""
GitHub Copilot requires the GitHub CLI with the Copilot extension:

Step 1: Install GitHub CLI
- Windows: winget install GitHub.cli OR download from cli.github.com
- Mac: brew install gh
- Linux: See cli.github.com for package manager instructions

Step 2: Authenticate
- Run: gh auth login
- Follow the prompts to authenticate with GitHub

Step 3: Install Copilot extension
- Run: gh extension install github/gh-copilot

Step 4: Subscription
- Requires GitHub Copilot subscription ($10/month individual)
- Free for verified students, teachers, and open source maintainers

To test: gh copilot suggest "hello world in python"
""",
        pricing=AIPricing(
            has_free_tier=True,
            free_tier_limits="FREE for students, teachers, and OSS maintainers",
            paid_price="$10/month individual, $19/month business",
            price_per_request="Unlimited requests with subscription",
            best_for="Students (FREE), active coders who want flat-rate pricing"
        )
    ),

    "codex": AIInstallInfo(
        name="OpenAI GPT/Codex",
        description="OpenAI's GPT models - versatile general-purpose AI",
        install_type="api_key",
        api_key_env="OPENAI_API_KEY",
        api_key_url="https://platform.openai.com/api-keys",
        cli_command=None,
        install_instructions="""
1. Go to https://platform.openai.com/
2. Sign up or log in
3. Go to API Keys section
4. Create a new API key
5. Set OPENAI_API_KEY environment variable
""",
        detailed_help="""
OpenAI GPT is accessed via the OpenAI API. Users need to:
1. Create an OpenAI account at platform.openai.com
2. Add billing information (pay-as-you-go, minimum $5)
3. Generate an API key from the API Keys section
4. Set the OPENAI_API_KEY environment variable

On Windows: setx OPENAI_API_KEY "sk-..."
On Mac/Linux: export OPENAI_API_KEY="sk-..."

The API key starts with "sk-". Costs vary by model ($0.002-0.06 per 1K tokens).
""",
        pricing=AIPricing(
            has_free_tier=False,
            free_tier_limits="CLI uses API credits - check platform.openai.com for current pricing",
            paid_price="Pay-as-you-go: $0.50-15/M tokens depending on model",
            price_per_request="Varies by model (GPT-4o cheaper than GPT-4)",
            best_for="Those in OpenAI ecosystem - check OpenAI for current offers"
        )
    ),
}


class AIInstaller:
    """Helps users install and configure AIs."""

    def __init__(self, config: Config):
        self.config = config
        self.orchestrator = Orchestrator(config)
        self.system = platform.system()

    def check_availability(self) -> dict[str, bool]:
        """Check which AIs are available."""
        return self.orchestrator.check_availability()

    def get_missing_ais(self) -> list[str]:
        """Get list of configured but unavailable AIs."""
        availability = self.check_availability()
        return [name for name, available in availability.items() if not available]

    def get_working_ai(self) -> Optional[str]:
        """Get the first working AI to use as helper."""
        availability = self.check_availability()
        for name, available in availability.items():
            if available:
                return name
        return None

    def get_install_info(self, ai_name: str) -> Optional[AIInstallInfo]:
        """Get installation info for an AI."""
        # Normalize name
        name_lower = ai_name.lower()
        for key, info in AI_INSTALL_INFO.items():
            if key in name_lower or name_lower in key:
                return info
        return None

    def check_env_var(self, var_name: str) -> bool:
        """Check if an environment variable is set."""
        return bool(os.environ.get(var_name))

    def check_cli_tool(self, command: str) -> bool:
        """Check if a CLI tool is installed."""
        try:
            result = subprocess.run(
                command.split(),
                capture_output=True,
                timeout=10
            )
            return result.returncode == 0
        except Exception:
            return False

    def open_api_key_page(self, ai_name: str) -> bool:
        """Open the API key page in browser."""
        info = self.get_install_info(ai_name)
        if info and info.api_key_url:
            webbrowser.open(info.api_key_url)
            return True
        return False

    def set_env_var_instructions(self, var_name: str, value: str = "<your-key>") -> str:
        """Get instructions for setting an environment variable."""
        if self.system == "Windows":
            return f'''
To set permanently (run in Command Prompt as Admin):
    setx {var_name} "{value}"

To set for current session only:
    set {var_name}={value}

After setting with setx, restart your terminal/application.
'''
        else:
            shell = os.environ.get("SHELL", "/bin/bash")
            if "zsh" in shell:
                rc_file = "~/.zshrc"
            else:
                rc_file = "~/.bashrc"
            return f'''
Add to {rc_file}:
    export {var_name}="{value}"

Then run:
    source {rc_file}

Or set for current session:
    export {var_name}="{value}"
'''

    def get_ai_assisted_help(
        self,
        helper_ai: str,
        target_ai: str,
        user_issue: str = "",
        callback: Optional[Callable[[str], None]] = None
    ) -> str:
        """Use a working AI to help install another AI."""
        info = self.get_install_info(target_ai)
        if not info:
            return f"No installation info available for {target_ai}"

        prompt = f"""You are helping a user install and configure {info.name} for use with Alloy (a multi-AI collaboration tool).

System: {self.system}

Installation Requirements:
{info.detailed_help}

Current Status:
- API Key Set: {self.check_env_var(info.api_key_env) if info.api_key_env else 'N/A'}
- CLI Tool Installed: {self.check_cli_tool(info.cli_command) if info.cli_command else 'N/A'}

User's Issue/Question: {user_issue if user_issue else "General setup help needed"}

Provide clear, step-by-step instructions to help them get {info.name} working. Be specific to their operating system ({self.system}). If they need to get an API key, tell them exactly where to go and what to click."""

        try:
            if self.config.streaming and callback:
                # Stream the response
                full_response = ""
                for chunk in self.orchestrator.query_streaming(helper_ai, prompt):
                    if isinstance(chunk, str):
                        full_response += chunk
                        callback(chunk)
                return full_response
            else:
                response = self.orchestrator.query(helper_ai, prompt)
                if response.success:
                    return response.content
                else:
                    return f"Error getting help: {response.error}"
        except Exception as e:
            return f"Error: {e}"

    def get_diagnostic_info(self) -> str:
        """Get diagnostic information about the system."""
        lines = [
            "=== Alloy AI Diagnostic Info ===",
            f"Operating System: {self.system} {platform.release()}",
            f"Python Version: {platform.python_version()}",
            "",
            "=== Environment Variables ===",
        ]

        for ai_name, info in AI_INSTALL_INFO.items():
            if info.api_key_env:
                is_set = self.check_env_var(info.api_key_env)
                status = "SET" if is_set else "NOT SET"
                lines.append(f"{info.api_key_env}: {status}")

        lines.extend(["", "=== CLI Tools ==="])

        for ai_name, info in AI_INSTALL_INFO.items():
            if info.cli_command:
                is_installed = self.check_cli_tool(info.cli_command)
                status = "INSTALLED" if is_installed else "NOT FOUND"
                lines.append(f"{info.name}: {status}")

        lines.extend(["", "=== AI Availability ==="])

        availability = self.check_availability()
        for name, available in availability.items():
            status = "AVAILABLE" if available else "UNAVAILABLE"
            lines.append(f"{name}: {status}")

        return "\n".join(lines)

    def get_pricing_info(self) -> str:
        """Get pricing information for all AIs."""
        lines = [
            "=== AI Pricing Comparison ===",
            "",
            "FREE TIER OPTIONS:",
            "-" * 40,
        ]

        # Free tier AIs first
        for ai_name, info in AI_INSTALL_INFO.items():
            if info.pricing and info.pricing.has_free_tier:
                lines.append(f"\n{info.name}")
                lines.append(f"  Free: {info.pricing.free_tier_limits}")
                lines.append(f"  Paid: {info.pricing.paid_price}")
                lines.append(f"  Best for: {info.pricing.best_for}")

        lines.extend([
            "",
            "PAID ONLY:",
            "-" * 40,
        ])

        # Paid only AIs
        for ai_name, info in AI_INSTALL_INFO.items():
            if info.pricing and not info.pricing.has_free_tier:
                lines.append(f"\n{info.name}")
                lines.append(f"  Price: {info.pricing.paid_price}")
                lines.append(f"  Per request: {info.pricing.price_per_request}")
                lines.append(f"  Best for: {info.pricing.best_for}")

        lines.extend([
            "",
            "RECOMMENDATION:",
            "-" * 40,
            "1. Start with Gemini (free tier) to try Alloy at no cost",
            "2. Students: GitHub Copilot is FREE for you!",
            "3. Check each provider's site for current pricing/offers",
            "",
            "Note: CLI tools use API credits. Pricing changes frequently.",
            "Always verify at the provider's website before committing.",
        ])

        return "\n".join(lines)

    def get_recommended_setup(self, budget: str = "free") -> list[str]:
        """Get recommended AIs based on budget.

        Args:
            budget: "free", "low" ($0-10/mo), "medium" ($10-50/mo), "unlimited"

        Returns:
            List of recommended AI names in priority order
        """
        if budget == "free":
            return ["gemini", "copilot"]  # Copilot free for students
        elif budget == "low":
            return ["gemini", "copilot", "claude"]
        elif budget == "medium":
            return ["claude", "gemini", "copilot", "codex"]
        else:  # unlimited
            return ["claude", "gemini", "codex", "copilot"]

    def get_pricing_summary_for_ai(self, ai_name: str) -> str:
        """Get a short pricing summary for an AI."""
        info = self.get_install_info(ai_name)
        if not info or not info.pricing:
            return "Pricing info not available"

        p = info.pricing
        if p.has_free_tier:
            return f"FREE TIER: {p.free_tier_limits}"
        else:
            return f"PAID: {p.price_per_request}"


def interactive_install(config: Config):
    """Run interactive installation assistant in CLI."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm

    console = Console()
    installer = AIInstaller(config)

    console.print(Panel.fit(
        "[bold]Alloy AI Installation Assistant[/bold]\n"
        "Let's get your AIs set up!",
        border_style="cyan"
    ))

    # Check current status
    console.print("\n[dim]Checking AI availability...[/dim]\n")
    availability = installer.check_availability()

    available = [n for n, a in availability.items() if a]
    missing = [n for n, a in availability.items() if not a]

    if available:
        console.print(f"[green]✓ Working:[/green] {', '.join(available)}")
    if missing:
        console.print(f"[yellow]✗ Need setup:[/yellow] {', '.join(missing)}")

    if not missing:
        console.print("\n[green]All configured AIs are working![/green]")
        return

    # Offer to help with missing AIs
    helper_ai = installer.get_working_ai()

    if helper_ai:
        console.print(f"\n[cyan]I can use {helper_ai} to help you set up the others.[/cyan]")

    for ai_name in missing:
        console.print(f"\n[bold]─── Setting up {ai_name} ───[/bold]")

        info = installer.get_install_info(ai_name)
        if not info:
            console.print(f"[dim]No installation info available for {ai_name}[/dim]")
            continue

        console.print(f"[dim]{info.description}[/dim]")

        # Show pricing
        if info.pricing:
            p = info.pricing
            if p.has_free_tier:
                console.print(f"[green]💰 FREE TIER:[/green] {p.free_tier_limits}")
            else:
                console.print(f"[yellow]💰 PAID:[/yellow] {p.price_per_request}")
            console.print(f"[dim]   Best for: {p.best_for}[/dim]")

        console.print(f"\n[bold]Quick Setup:[/bold]")
        console.print(info.install_instructions)

        # Offer to open API key page
        if info.api_key_url:
            if Confirm.ask(f"Open {info.name} API key page in browser?"):
                installer.open_api_key_page(ai_name)
                console.print("[dim]Browser opened. Get your API key and come back.[/dim]")

                if info.api_key_env:
                    console.print(f"\n[bold]Set your API key:[/bold]")
                    console.print(installer.set_env_var_instructions(info.api_key_env))

        # Offer AI-assisted help
        if helper_ai:
            if Confirm.ask(f"Want {helper_ai} to help troubleshoot?"):
                issue = Prompt.ask("Describe your issue (or press Enter for general help)")
                console.print(f"\n[dim]Asking {helper_ai} for help...[/dim]\n")

                help_text = installer.get_ai_assisted_help(
                    helper_ai,
                    ai_name,
                    issue,
                    callback=lambda chunk: console.print(chunk, end="")
                )
                console.print()  # Newline after streaming

    # Final check
    console.print("\n[dim]Re-checking availability...[/dim]")
    new_availability = installer.check_availability()
    new_available = [n for n, a in new_availability.items() if a]

    if len(new_available) > len(available):
        console.print(f"\n[green]Great! Now working: {', '.join(new_available)}[/green]")
    else:
        console.print("\n[yellow]After setting environment variables, restart your terminal and try again.[/yellow]")


if __name__ == "__main__":
    config = Config.load()
    interactive_install(config)
