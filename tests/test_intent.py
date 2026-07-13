"""Tests for command-bar intent classification and routing extraction."""

from __future__ import annotations

import pytest

from archon.intent import Intent, classify


# --------------------------------------------------------------------------- #
# PR_REVIEW
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "text,pr",
    [
        ("review PR #123", 123),
        ("review pull request 45", 45),
        ("can you review pr 7 on repo xyz", 7),
        ("look at PR#9", 9),
    ],
)
def test_pr_review_detected(text, pr):
    r = classify(text)
    assert r.intent is Intent.PR_REVIEW, r.rationale
    assert r.pr_number == pr


def test_pr_substring_is_not_pr_review():
    # "PR" appears but this is a feature request, not a review of a PR.
    r = classify("implement a PR preview feature")
    assert r.intent is Intent.FEATURE, r.rationale
    assert r.pr_number is None


# --------------------------------------------------------------------------- #
# NEW_PROJECT
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "text",
    [
        "start a new project: a CLI todo app in python",
        "create a new repo called invoice-parser",
        "scaffold a brand new project from scratch",
        "bootstrap a new fastapi service named billing",
    ],
)
def test_new_project_detected(text):
    r = classify(text)
    assert r.intent is Intent.NEW_PROJECT, r.rationale
    assert r.message  # a usable description is carried through


def test_new_project_extracts_name():
    r = classify("create a new repo called invoice-parser")
    assert r.intent is Intent.NEW_PROJECT
    assert r.project_name and "invoice" in r.project_name.lower()


def test_new_project_explicit_name_keeps_all_tokens():
    # An explicit "called <name>" must not have tokens like "cli" stripped.
    r = classify("start a new project called todo-cli: a cli todo app")
    assert r.intent is Intent.NEW_PROJECT
    assert r.project_name == "todo-cli"


# --------------------------------------------------------------------------- #
# MESSAGE_TO_JOB
# --------------------------------------------------------------------------- #

def test_message_to_job_by_id():
    r = classify("tell job#12 to also update the README")
    assert r.intent is Intent.MESSAGE_TO_JOB, r.rationale
    assert r.job_ref and "12" in r.job_ref


def test_message_to_job_by_known_title():
    r = classify(
        "reply to the auth-refactor job: yes proceed",
        known_job_titles=["auth-refactor regen"],
    )
    assert r.intent is Intent.MESSAGE_TO_JOB, r.rationale
    assert r.job_ref


def test_message_to_job_by_at_handle():
    r = classify("@rebate-scenarios use the new endpoint",
                 known_job_titles=["rebate scenarios"])
    assert r.intent is Intent.MESSAGE_TO_JOB, r.rationale


def test_feature_not_hijacked_as_message():
    r = classify("add dark mode to the settings page")
    assert r.intent is Intent.FEATURE, r.rationale


# --------------------------------------------------------------------------- #
# FEATURE fallback
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "text",
    [
        "implement a hello endpoint",
        "fix why A1 stopped regenerating",
        "add dark mode to the settings page",
    ],
)
def test_feature_fallback(text):
    r = classify(text)
    assert r.intent is Intent.FEATURE, r.rationale
    assert r.message == text


# --------------------------------------------------------------------------- #
# LLM tie-breaker
# --------------------------------------------------------------------------- #

def test_llm_breaks_low_confidence_tie():
    # Ambiguous-ish input; a stub llm forces PR_REVIEW.
    r = classify("that thing from earlier", llm=lambda t: "pr_review")
    assert r.intent is Intent.PR_REVIEW


def test_llm_does_not_override_high_confidence():
    r = classify("review PR #5", llm=lambda t: "feature")
    assert r.intent is Intent.PR_REVIEW
    assert r.pr_number == 5
