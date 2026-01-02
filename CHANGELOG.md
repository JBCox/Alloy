# Changelog

All notable changes to Alloy are documented in this file.

## [Unreleased]

### Added

#### GUI Improvements
- **Tooltip System** - Hover tooltips on all settings for better UX
- **Step Indicator** - Visual progress indicator in setup wizard (Step 2 of 5 with dots)
- **Focus Indicators** - Visual feedback when widgets are focused
- **Hover States** - Enhanced button and widget hover effects
- **Cross-Platform Fonts** - Automatic font selection for Windows/macOS/Linux
- **Keyboard Shortcuts** - Ctrl+S to save, Escape to cancel, Enter for next in wizard

#### New Settings Tabs
- **Context Tab** - Conversation history and session saving settings
- **Advanced Tab** - Config file management, import/export, reset to defaults

#### New Configuration Options
- `timeout` - Global timeout in seconds (30-600)
- `max_concurrent_ais` - Limit parallel AI queries (1-10)
- `retry_count` - Number of retries on failure (0-5)
- `retry_delay` - Seconds between retries (1-10)
- `verbose_mode` - Output verbosity (silent/normal/verbose/debug)
- `preview_length` - History preview truncation length
- `save_conversations` - Auto-save conversation sessions
- `save_path` - Directory for saved conversations
- `satisfaction_threshold` - Quality score for --until=satisfied (1-10)
- `max_rounds` - Safety cap on collaboration rounds (1-20)
- `voting_system` - Consensus method (majority/unanimous/weighted)

#### Per-AI Settings
- `timeout` - Per-AI timeout override
- `retry_count` - Per-AI retry count
- `weight` - Voting weight for consensus decisions
- `fallback_ai` - Backup AI if primary fails

#### New Commands
- `/reload` - Reload configuration without restarting

#### Advanced Tab Features
- View and open config file path
- Open in system default editor
- Open config folder in file explorer
- Export configuration to file
- Import configuration from file
- Reset all settings to defaults

### Fixed
- **Mousewheel Bug** - Fixed global mousewheel binding interfering with other widgets
- **Empty AI List Crash** - Setup wizard now handles no detected AIs gracefully
- **Template Rename Data Loss** - Fixed data loss when renaming custom role templates
- **YAML Validation** - Added proper validation and error handling for malformed config files
- **Button Bar Order** - Standardized button placement (Cancel left, Save right)

### Changed
- Reorganized settings into logical sections with subheadings
- Improved visual card styling for AI configuration
- Enhanced scrollable frame behavior
- Better error messages for configuration issues

## [0.1.0] - Initial Release

### Added
- Multi-AI collaboration CLI
- Streaming response support
- Collaboration modes: roundtable, chain, brainstorm, devils-advocate, roles, code-review, solve
- Mode options: rounds, order, judge, template
- Flags: --checkpoint, --until=satisfied, --until=consensus, --implement, --parallel
- GUI setup wizard
- GUI settings editor
- Autocomplete for commands, modes, AI names, and flags
- Conversation history with context management
- YAML-based configuration
- Support for Claude, Gemini, and Copilot CLIs
