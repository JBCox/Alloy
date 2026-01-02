# Alloy

**Multiple AIs, stronger together.**

Alloy is a powerful CLI tool that orchestrates multiple AI assistants (Claude, Gemini, Copilot, and more) to work together on complex tasks. By leveraging different AI perspectives, Alloy delivers more robust, well-reasoned solutions than any single AI alone.

## Features

- **Multi-AI Collaboration** - Query multiple AIs simultaneously or sequentially
- **Collaboration Modes** - Specialized modes for different types of tasks (brainstorming, code review, debate, etc.)
- **Streaming Responses** - Real-time output as AIs generate responses
- **Conversation History** - Maintain context across multiple interactions
- **GUI Setup Wizard** - Easy first-time configuration
- **Settings Editor** - Visual configuration with tooltips and validation
- **Extensible** - Add custom AIs and role templates

## Installation

### Prerequisites

- Python 3.10+
- One or more AI CLIs installed:
  - [Claude Code](https://github.com/anthropics/claude-code) (`claude`)
  - [Gemini CLI](https://github.com/google-gemini/gemini-cli) (`gemini`)
  - [GitHub Copilot CLI](https://docs.github.com/en/copilot/using-github-copilot/using-github-copilot-in-the-command-line) (`copilot`)

### Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/yourusername/alloy.git
   cd alloy
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run the setup wizard (first time):
   ```bash
   python main.py --setup
   ```

4. Or start directly:
   ```bash
   python main.py
   ```

## Quick Start

```bash
# Start Alloy
python main.py

# Query the default AI
> What is the capital of France?

# Query a specific AI
> @claude Explain quantum computing

# Query all AIs
> @all What are the benefits of microservices?

# Use a collaboration mode
> @roundtable Should we use React or Vue for this project?

# Use mode options
> @chain[rounds=3] Improve this function: def add(a,b): return a+b
```

## Commands

### System Commands

| Command | Description |
|---------|-------------|
| `/help` | Show help and available commands |
| `/ais` | List configured AIs and their status |
| `/modes` | Show available collaboration modes |
| `/settings` | Open the GUI settings editor |
| `/reload` | Reload configuration without restarting |
| `/history` | Show conversation history |
| `/clear` | Clear conversation history |
| `/quit` | Exit Alloy |

### AI Selection

| Syntax | Description |
|--------|-------------|
| `@claude` | Query Claude specifically |
| `@gemini` | Query Gemini specifically |
| `@copilot` | Query GitHub Copilot specifically |
| `@all` | Query all enabled AIs |

## Collaboration Modes

### @roundtable
Full-context discussion where each AI sees and responds to all previous responses.
```
> @roundtable What's the best approach for caching in a distributed system?
```

### @chain
Sequential refinement where each AI improves on the previous response.
```
> @chain[rounds=3] Write a haiku about programming
```

### @brainstorm
Parallel idea generation - all AIs respond independently, then ideas are synthesized.
```
> @brainstorm Features for a new productivity app
```

### @devils-advocate
Structured debate with steelman arguments, attacks, and final verdict.
```
> @devils-advocate Should we migrate to a monorepo?
```

### @roles
Role-based collaboration using templates or custom roles.
```
> @roles[template=architecture] Design a notification system
```

**Built-in Templates:**
- `architecture` - Architect, Critic, Implementer
- `debate` - Advocate, Opponent, Moderator
- `review` - Author, Reviewer, Editor
- `research` - Researcher, Skeptic, Synthesizer

### @code-review
Multi-perspective code review with parallel analysis.
```
> @code-review Review this pull request: [code]
```

### @solve
Collaborative problem solving with phases: Understand, Plan, Implement.
```
> @solve[--implement] Create a rate limiter in Python
```

## Mode Options

Options are specified in brackets after the mode name:

| Option | Description | Example |
|--------|-------------|---------|
| `rounds=N` | Number of discussion rounds | `@chain[rounds=5]` |
| `order=a,b,c` | Specify AI order | `@chain[order=claude,gemini]` |
| `judge=ai` | AI to judge/synthesize | `@brainstorm[judge=claude]` |
| `template=name` | Role template to use | `@roles[template=debate]` |

### Flags

| Flag | Description |
|------|-------------|
| `--checkpoint` | Pause after each round for review |
| `--until=satisfied` | Continue until quality threshold met |
| `--until=consensus` | Continue until AIs agree |
| `--implement` | Generate implementation code at end |
| `--parallel` | Run queries in parallel |

## Configuration

Alloy uses a YAML configuration file. Default location: `./config.yaml`

### AI Configuration

```yaml
default_ai: claude
default_judge: ""  # Empty = use default_ai

ais:
  claude:
    command: "claude -p {message}"
    color: cyan
    enabled: true
    description: "Anthropic's Claude Code CLI"
    timeout: 0        # 0 = use global timeout
    retry_count: 0
    weight: 1         # Voting weight
    fallback_ai: ""   # Backup if this AI fails

  gemini:
    command: "gemini -p {message}"
    color: blue
    enabled: true
    description: "Google's Gemini CLI"
```

### Display Settings

```yaml
display:
  show_timestamps: true
  show_ai_header: true
  max_width: 120           # 0 = no limit
  verbose_mode: normal     # silent, normal, verbose, debug
  preview_length: 100      # History preview truncation
```

### Execution Settings

```yaml
execution:
  streaming: true          # Show output as it generates
  parallel: false          # Global parallel execution
  refresh_rate: 10         # Streaming updates per second
  timeout: 300             # Global timeout (seconds)
  max_concurrent_ais: 3    # Max parallel AI queries
  retry_count: 0           # Retry failed queries
  retry_delay: 2           # Seconds between retries
```

### Context Settings

```yaml
context:
  max_history: 50          # Max messages in history
  auto_context: 0          # Auto-include last N messages (0 = disabled)
  save_conversations: false
  save_path: ""            # Directory for saved conversations
```

### Mode Settings

```yaml
modes:
  roundtable:
    rounds: 1
    parallel: false
  chain:
    rounds: 3
    parallel: false
  brainstorm:
    rounds: 1
    parallel: true

  global:
    satisfaction_threshold: 8   # Quality score for --until=satisfied
    max_rounds: 10              # Safety cap
    voting_system: majority     # majority, unanimous, weighted
```

### Custom Role Templates

```yaml
custom_templates:
  security:
    roles:
      attacker: "Find vulnerabilities and attack vectors"
      defender: "Design mitigations and security measures"
      auditor: "Evaluate the security posture objectively"
```

## GUI

### Setup Wizard

Run the setup wizard for first-time configuration:
```bash
python main.py --setup
```

The wizard will:
1. Detect installed AI CLIs
2. Configure AI settings
3. Set preferences
4. Generate `config.yaml`

### Settings Editor

Open the settings editor anytime:
```bash
# From the CLI
> /settings

# Or directly
python -m gui.settings
```

**Tabs:**
- **AIs** - Configure AI CLIs, colors, and per-AI settings
- **Display** - Timestamps, verbosity, output formatting
- **Execution** - Streaming, parallel, timeouts, retries
- **Context** - History limits, conversation saving
- **Modes** - Default rounds, parallel settings, global options
- **Templates** - Create and edit custom role templates
- **Advanced** - Config file management, import/export, reset

## Examples

### Compare AI Responses
```
> @all Explain the difference between REST and GraphQL
```

### Iterative Refinement
```
> @chain[rounds=5] Write a Python function to find the longest palindrome
```

### Architecture Discussion
```
> @roles[template=architecture] Design an event-driven microservices architecture
```

### Code Review
```
> @code-review --parallel
def quick_sort(arr):
    if len(arr) <= 1:
        return arr
    pivot = arr[0]
    less = [x for x in arr[1:] if x < pivot]
    greater = [x for x in arr[1:] if x >= pivot]
    return quick_sort(less) + [pivot] + quick_sort(greater)
```

### Debate Mode
```
> @devils-advocate Should we adopt Kubernetes for our startup?
```

### Problem Solving with Implementation
```
> @solve[--implement] Create a thread-safe LRU cache in Python
```

## Keyboard Shortcuts

### CLI
- `Tab` - Autocomplete commands, modes, AI names
- `Ctrl+C` - Cancel current operation
- `Ctrl+D` - Exit Alloy

### Settings Editor
- `Ctrl+S` - Save settings
- `Escape` - Cancel/close

### Setup Wizard
- `Enter` - Next page
- `Escape` - Cancel wizard

## Requirements

```
rich>=13.0.0
prompt_toolkit>=3.0.0
pyyaml>=6.0
```

## License

MIT License

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
