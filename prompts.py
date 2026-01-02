"""Prompt templates for AI Collab collaboration modes."""

from typing import Optional


class ModePrompts:
    """Prompt templates for different collaboration modes."""

    # ===== ROUNDTABLE =====

    ROUNDTABLE_INITIAL = """We're having a collaborative roundtable discussion about:

{topic}

You are participating in this discussion. Share your perspective, insights, and any relevant considerations. Be constructive and build upon what others have said.

Previous contributions in this discussion:
{context}

Now share your perspective."""

    ROUNDTABLE_FIRST = """We're having a collaborative roundtable discussion about:

{topic}

You are the first to speak. Share your initial perspective, insights, and relevant considerations on this topic."""

    # ===== CHAIN =====

    CHAIN_INITIAL = """Here is the task or topic:

{topic}

Please provide your response or solution to this."""

    CHAIN_REFINE = """Here is the original task:

{topic}

Here is the previous response:

---
{previous}
---

Please review and improve upon this response. Focus on:
- Enhancing clarity and completeness
- Fixing any errors or issues
- Adding anything that was missed
- Improving the overall quality

Provide your refined version."""

    CHAIN_DISAGREE = """You suggested a different approach than the previous response.

Previous approach:
{previous_approach}

Your approach:
{current_approach}

Please either:
1. Explain why your approach is better and should be preferred, OR
2. Synthesize both approaches into a unified, improved solution

Provide a clear resolution."""

    # ===== BRAINSTORM =====

    BRAINSTORM_GENERATE = """We're brainstorming ideas about:

{topic}

Generate 3-5 creative, practical ideas. For each idea:
- Give it a short name/title
- Explain it in 1-2 sentences
- Note any key advantages

Focus on quantity and creativity. Don't self-censor - include both conventional and unconventional ideas."""

    BRAINSTORM_MERGE = """We've gathered ideas from multiple sources about:

{topic}

Here are all the ideas collected:

{all_ideas}

Now please:
1. Identify any duplicate or very similar ideas and merge them
2. Rank the top 5 most promising ideas
3. For each top idea, suggest one enhancement or combination with another idea

Provide a consolidated, prioritized list of the best ideas."""

    # ===== DEVIL'S ADVOCATE =====

    DEVILS_ADVOCATE_STEELMAN = """Here's a proposal or idea to evaluate:

{topic}

Present the STRONGEST possible case FOR this idea. Be a true advocate:
- Explain why this is a good idea
- Highlight the benefits and advantages
- Address potential objections preemptively
- Give the most charitable interpretation

Make the best possible case."""

    DEVILS_ADVOCATE_ATTACK = """Here's the proposal:

{topic}

And here's the case made in favor:

{steelman}

Now play devil's advocate. Present the STRONGEST case AGAINST this idea:
- Identify weaknesses, risks, and downsides
- Find flaws in the pro arguments
- Raise concerns that weren't addressed
- Consider edge cases and failure modes

Be thorough but fair - focus on legitimate concerns, not nitpicking."""

    DEVILS_ADVOCATE_VERDICT = """Here's a proposal that was debated:

{topic}

Case FOR:
{steelman}

Case AGAINST:
{attack}

Now provide a balanced verdict:
1. Summarize the strongest points on each side
2. Identify which concerns are most legitimate
3. Suggest modifications that address the key concerns
4. Give a final recommendation with caveats

Be fair and constructive."""

    # ===== ROLES =====

    ROLE_PROMPT = """You are taking on the role of: {role_name}

Your role instructions: {role_description}

The topic/task is:
{topic}

{context}

Respond from your role's perspective. Stay in character and provide the specific contribution expected from your role."""

    # ===== CODE REVIEW =====

    CODE_REVIEW_SECURITY = """Review the following code for SECURITY issues:

```
{code}
```

Focus specifically on:
- Input validation vulnerabilities
- Injection risks (SQL, XSS, command injection, etc.)
- Authentication/authorization flaws
- Data exposure risks
- Cryptographic issues

For each issue found, provide:
- Severity (Critical/High/Medium/Low)
- Location in code
- Description of the vulnerability
- Suggested fix

If no security issues found, note areas that were checked."""

    CODE_REVIEW_PERFORMANCE = """Review the following code for PERFORMANCE issues:

```
{code}
```

Focus specifically on:
- Algorithm efficiency (time/space complexity)
- Resource usage (memory, connections, handles)
- Caching opportunities
- N+1 query problems
- Unnecessary operations

For each issue found, provide:
- Impact (High/Medium/Low)
- Location in code
- Description of the issue
- Suggested optimization

If no performance issues found, note what was analyzed."""

    CODE_REVIEW_QUALITY = """Review the following code for CODE QUALITY issues:

```
{code}
```

Focus specifically on:
- Code readability and clarity
- Naming conventions
- Code structure and organization
- Error handling
- Documentation/comments
- Best practices for the language

For each issue found, provide:
- Severity (High/Medium/Low)
- Location in code
- Description
- Suggested improvement

If no quality issues found, note what looks good."""

    CODE_REVIEW_AGGREGATE = """Multiple reviewers have analyzed this code:

{code}

Here are their findings:

{reviews}

Please:
1. Deduplicate any issues mentioned by multiple reviewers
2. Prioritize all issues by severity/impact
3. Create a consolidated action list
4. Note any areas where reviewers disagreed

Provide a final, organized review summary."""

    # ===== SOLVE =====

    SOLVE_UNDERSTAND = """We need to solve this problem:

{topic}

PHASE 1: UNDERSTANDING

Before proposing solutions, let's fully understand the problem:
1. What exactly is the problem asking?
2. What are the inputs and expected outputs?
3. What constraints or requirements exist?
4. What edge cases should we consider?
5. Are there any ambiguities that need clarification?

Provide a clear problem analysis."""

    SOLVE_PLAN = """Here's the problem we're solving:

{topic}

Our understanding of the problem:
{understanding}

PHASE 2: PLANNING

Now let's design a solution:
1. What approach will we take and why?
2. What are the key steps or components?
3. What data structures/algorithms are needed?
4. What are potential pitfalls to avoid?
5. How will we verify the solution works?

Provide a clear solution plan."""

    SOLVE_IMPLEMENT = """Here's the problem:

{topic}

Our plan:
{plan}

PHASE 3: IMPLEMENTATION

Now implement the solution:
1. Write the actual code or detailed implementation
2. Include necessary error handling
3. Add brief comments for complex logic
4. Show example usage if applicable

Provide the complete implementation."""

    # ===== QUALITY CHECKING =====

    JUDGE_QUALITY = """Rate the quality of this response on a scale of 1-10:

Original task:
{topic}

Response to evaluate:
{response}

Consider:
- Completeness: Does it fully address the task?
- Correctness: Is the information/solution accurate?
- Clarity: Is it well-organized and easy to understand?
- Quality: Is the writing/code quality high?

Provide:
1. A single number rating (1-10)
2. Brief justification (1-2 sentences)
3. One specific improvement suggestion

Format your response starting with "Rating: X/10" on the first line."""

    JUDGE_CONSENSUS = """Analyze whether these AI responses reach consensus:

Topic:
{topic}

Responses:
{responses}

Determine:
1. Do they fundamentally agree on the core answer/approach? (yes/partially/no)
2. What key points do they agree on?
3. What points do they disagree on?
4. Is the disagreement substantive or superficial?

Start your response with "Consensus: YES", "Consensus: PARTIAL", or "Consensus: NO"."""


def get_roundtable_prompt(topic: str, context: str = "", is_first: bool = False) -> str:
    """Get roundtable discussion prompt."""
    if is_first:
        return ModePrompts.ROUNDTABLE_FIRST.format(topic=topic)
    return ModePrompts.ROUNDTABLE_INITIAL.format(topic=topic, context=context)


def get_chain_prompt(topic: str, previous: Optional[str] = None) -> str:
    """Get chain refinement prompt."""
    if previous is None:
        return ModePrompts.CHAIN_INITIAL.format(topic=topic)
    return ModePrompts.CHAIN_REFINE.format(topic=topic, previous=previous)


def get_brainstorm_prompt(topic: str, phase: str = "generate", all_ideas: str = "") -> str:
    """Get brainstorm prompt for given phase."""
    if phase == "generate":
        return ModePrompts.BRAINSTORM_GENERATE.format(topic=topic)
    return ModePrompts.BRAINSTORM_MERGE.format(topic=topic, all_ideas=all_ideas)


def get_devils_advocate_prompt(topic: str, phase: str, steelman: str = "", attack: str = "") -> str:
    """Get devil's advocate prompt for given phase."""
    if phase == "steelman":
        return ModePrompts.DEVILS_ADVOCATE_STEELMAN.format(topic=topic)
    elif phase == "attack":
        return ModePrompts.DEVILS_ADVOCATE_ATTACK.format(topic=topic, steelman=steelman)
    else:  # verdict
        return ModePrompts.DEVILS_ADVOCATE_VERDICT.format(topic=topic, steelman=steelman, attack=attack)


def get_role_prompt(role_name: str, role_description: str, topic: str, context: str = "") -> str:
    """Get role-based prompt."""
    return ModePrompts.ROLE_PROMPT.format(
        role_name=role_name,
        role_description=role_description,
        topic=topic,
        context=context if context else "You are the first to contribute."
    )


def get_code_review_prompt(code: str, review_type: str) -> str:
    """Get code review prompt for specific review type."""
    prompts = {
        "security": ModePrompts.CODE_REVIEW_SECURITY,
        "performance": ModePrompts.CODE_REVIEW_PERFORMANCE,
        "quality": ModePrompts.CODE_REVIEW_QUALITY,
    }
    template = prompts.get(review_type, ModePrompts.CODE_REVIEW_QUALITY)
    return template.format(code=code)


def get_code_review_aggregate_prompt(code: str, reviews: str) -> str:
    """Get prompt for aggregating code reviews."""
    return ModePrompts.CODE_REVIEW_AGGREGATE.format(code=code, reviews=reviews)


def get_solve_prompt(topic: str, phase: str, understanding: str = "", plan: str = "") -> str:
    """Get solve prompt for given phase."""
    if phase == "understand":
        return ModePrompts.SOLVE_UNDERSTAND.format(topic=topic)
    elif phase == "plan":
        return ModePrompts.SOLVE_PLAN.format(topic=topic, understanding=understanding)
    else:  # implement
        return ModePrompts.SOLVE_IMPLEMENT.format(topic=topic, plan=plan)


def get_judge_quality_prompt(topic: str, response: str) -> str:
    """Get prompt for judging response quality."""
    return ModePrompts.JUDGE_QUALITY.format(topic=topic, response=response)


def get_judge_consensus_prompt(topic: str, responses: str) -> str:
    """Get prompt for judging consensus."""
    return ModePrompts.JUDGE_CONSENSUS.format(topic=topic, responses=responses)
