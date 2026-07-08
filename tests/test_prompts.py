from archon import prompts


def test_pr_review_prompt_has_safety_rules():
    p = prompts.pr_review_prompt(pr_number=552, repo_name="ci_amplify_ai",
                                 provider_name="Claude Code CLI",
                                 worktree_path="/w/pr", branch="review/pr-552/claude")
    assert "PR #552" in p
    assert "Do not submit a GitHub review until the human approves." in p
    assert "final recommendation" in p
    assert "review/pr-552/claude" in p


def test_feature_prompt_has_safety_rules():
    p = prompts.feature_prompt(feature_name="newButton4User", repo_name="ci_amplify_ai",
                               provider_name="Claude Code CLI", worktree_path="/w/feat",
                               branch="feature/newButton4User")
    assert "newButton4User" in p
    assert "Do not create a PR until the human approves." in p
    assert "Work only in this branch/worktree." in p


def test_feature_prompt_uses_description_when_given():
    p = prompts.feature_prompt(feature_name="x", repo_name="r", provider_name="p",
                               worktree_path="/w", branch="b",
                               feature_description="Add a shiny button")
    assert "Add a shiny button" in p


def test_comparison_prompt_forbids_destructive_actions():
    p = prompts.comparison_prompt("run A: ...\nrun B: ...")
    assert "Do not merge, push, submit a review, or delete worktrees." in p
