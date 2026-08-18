# ai-chat desktop launcher: prompts for a topic, then runs the relay in this window.
# ASCII only: powershell 5.1 reads BOM-less .ps1 files as ANSI.
$Host.UI.RawUI.WindowTitle = "AI Chat - Claude, GPT & Gemini"
Write-Host ""
Write-Host "  AI Chat" -ForegroundColor White
Write-Host "  Claude, GPT and Gemini talk to each other. Type anytime to interject." -ForegroundColor DarkGray
Write-Host ""
$topic = Read-Host "  What should they talk about"
if ([string]::IsNullOrWhiteSpace($topic)) { exit }
$turns = Read-Host "  Max rounds (Enter for 10)"
if ($turns -notmatch '^\d+$') { $turns = 10 }
Write-Host ""
& python "C:\ai-chat\relay.py" $topic --turns $turns
Write-Host ""
Read-Host "  Press Enter to close" | Out-Null
