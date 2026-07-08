"""Default task prompts injected into provider panes/runs.

Every prompt embeds Archon's safety rules (no PR creation / external submission
without a human) so the guarantees hold regardless of provider.
"""

from __future__ import annotations

PR_REVIEW_TEMPLATE = """\
You are reviewing PR #{pr_number} in {repo_name}.

Provider: {provider_name}
Worktree: {worktree_path}
Branch: {branch}

Rules:
- Do not modify files unless explicitly asked.
- Do not submit a GitHub review until the human approves.
- Review the PR diff against the base branch.
- Use gh pr view #{pr_number} and gh pr diff #{pr_number} as needed.
- Inspect changed files directly.
- Run focused tests only when helpful.

Look for:
- correctness bugs
- auth or data-access mistakes
- security issues
- broken types
- bad generated code
- missing tests
- regressions
- maintainability issues

Produce:
1. executive summary
2. must-fix issues
3. nice-to-fix issues
4. tests run and results
5. suggested GitHub review comments
6. final recommendation: approve / comment / request changes
"""

FEATURE_TEMPLATE = """\
Implement feature `{feature_name}` in {repo_name}.

Provider: {provider_name}
Worktree: {worktree_path}
Branch: {branch}

Rules:
- Work only in this branch/worktree.
- Keep the diff focused and minimal.
- First inspect the project structure before editing.
- Find the correct frontend/backend locations before making changes.
- Follow nearby code patterns.
- Add or update tests if the repo has a nearby test pattern.
- Run the smallest useful validation commands.
- Do not create a PR until the human approves.

Feature request:
{feature_description}

At the end, summarize:
1. files changed
2. behavior added
3. tests run
4. risks / follow-up questions
5. exact commands the human should run next
"""

COMPARISON_TEMPLATE = """\
Compare the outputs from these provider runs:

{provider_run_summaries}

Produce:
1. agreements
2. disagreements
3. highest-confidence issues
4. suspicious or low-quality findings
5. recommended next action

Do not merge, push, submit a review, or delete worktrees.
"""


def pr_review_prompt(
    *,
    pr_number: int,
    repo_name: str,
    provider_name: str,
    worktree_path: str,
    branch: str,
) -> str:
    return PR_REVIEW_TEMPLATE.format(
        pr_number=pr_number,
        repo_name=repo_name,
        provider_name=provider_name,
        worktree_path=worktree_path,
        branch=branch,
    )


def feature_prompt(
    *,
    feature_name: str,
    repo_name: str,
    provider_name: str,
    worktree_path: str,
    branch: str,
    feature_description: str | None = None,
) -> str:
    return FEATURE_TEMPLATE.format(
        feature_name=feature_name,
        repo_name=repo_name,
        provider_name=provider_name,
        worktree_path=worktree_path,
        branch=branch,
        feature_description=feature_description or feature_name,
    )


def comparison_prompt(provider_run_summaries: str) -> str:
    return COMPARISON_TEMPLATE.format(provider_run_summaries=provider_run_summaries)
