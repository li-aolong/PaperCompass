"""v3 lint tests.

Plan lint now validates v3 fields:
  - search_hints: ≥3 critical, ≥5 ideal
  - search_keyword_text: warn if missing or <20 words
  - judge_examples: ≥3 in_scope and ≥3 out_of_scope critical
  - seed_papers: optional suggestions; missing seed coverage is filled by source-backed bootstrap
"""

from papercompass.auto.lint import (
    LintIssue,
    filter_hallucinated_decisions,
    format_issues_for_prompt,
    issues_summary,
    lint_plan_output,
    repair_plan_seed_contract,
)


def _has(issues, code):
    return any(i.code == code for i in issues)


def _critical(issues):
    return [i for i in issues if i.is_critical()]


def _good_plan() -> dict:
    return {
        "search_hints": ["a", "b", "c", "d", "e", "f"],
        "search_keyword_text": " ".join(f"kw{i}" for i in range(40)),
        "judge_examples": {
            "in_scope": [
                {"title": "in1", "reason": "r"},
                {"title": "in2", "reason": "r"},
                {"title": "in3", "reason": "r"},
            ],
            "out_of_scope": [
                {"title": "out1", "reason": "r"},
                {"title": "out2", "reason": "r"},
                {"title": "out3", "reason": "r"},
            ],
        },
        "seed_papers": [
            {"title": f"s{i}", "paper_role": "core_method", "required": True}
            for i in range(3)
        ],
    }


def test_lint_passes_minimum_v3_plan():
    issues = lint_plan_output(_good_plan())
    assert not _critical(issues)


def test_lint_flags_too_few_search_hints():
    plan = _good_plan() | {"search_hints": ["a", "b"]}
    issues = lint_plan_output(plan)
    assert _has(issues, "search_hints_too_few")


def test_lint_warns_on_thin_search_hints():
    plan = _good_plan() | {"search_hints": ["a", "b", "c"]}
    issues = lint_plan_output(plan)
    assert _has(issues, "search_hints_thin")


def test_lint_warns_on_missing_keyword_text():
    plan = _good_plan() | {"search_keyword_text": ""}
    issues = lint_plan_output(plan)
    assert _has(issues, "no_keyword_text")


def test_lint_warns_on_short_keyword_text():
    plan = _good_plan() | {"search_keyword_text": "five word paragraph here only"}
    issues = lint_plan_output(plan)
    assert _has(issues, "keyword_text_short")


def test_lint_flags_too_few_in_scope_examples():
    plan = _good_plan()
    plan["judge_examples"]["in_scope"] = [{"title": "x", "reason": "r"}]
    issues = lint_plan_output(plan)
    assert _has(issues, "in_scope_examples_too_few")


def test_lint_flags_too_few_out_of_scope_examples():
    plan = _good_plan()
    plan["judge_examples"]["out_of_scope"] = []
    issues = lint_plan_output(plan)
    assert _has(issues, "out_of_scope_examples_too_few")


def test_lint_ignores_brain_seed_count():
    plan = _good_plan() | {"seed_papers": [{"title": "x", "paper_role": "core_method", "required": True}]}
    issues = lint_plan_output(plan)
    assert not _has(issues, "seeds_too_few")


def test_lint_ignores_brain_seed_contract_fields():
    plan = _good_plan()
    plan["seed_papers"] = [
        {"title": "x", "required": True},
        {"title": "y", "paper_role": "core_method"},
        {"title": "z", "paper_role": "background_anchor", "required": True},
    ]
    issues = lint_plan_output(plan)
    assert not _has(issues, "seed_roles_missing")
    assert not _has(issues, "seed_required_missing")


def test_lint_rejects_non_dict():
    issues = lint_plan_output(["not a dict"])
    assert _critical(issues)
    assert _has(issues, "plan_not_dict")


# ── filter_hallucinated_decisions (used by brain_score and friends) ──


def test_filter_drops_hallucinated_keys():
    queue_keys = {"k1", "k2"}
    rows = [
        {"candidate_key": "k1", "decision": "accept"},
        {"candidate_key": "x", "decision": "accept"},
        {"candidate_key": "k2", "decision": "reject"},
    ]
    cleaned, lint = filter_hallucinated_decisions(rows, queue_keys)
    assert {r["candidate_key"] for r in cleaned} == queue_keys
    assert any(i.code == "dropped_hallucinated_key" for i in lint)


def test_filter_passes_through_empty_key():
    """Empty key triggers downstream title-match fallback; lint must keep them."""
    cleaned, lint = filter_hallucinated_decisions(
        [{"candidate_key": "", "title": "T"}, {"candidate_key": "k1"}],
        {"k1"},
    )
    assert len(cleaned) == 2
    assert not lint


def test_filter_remaps_dblp_keys_with_dropped_trailing_semicolon():
    queue_key = "dblp:367/3657;;;62/5860-6;51/8073;"
    cleaned, lint = filter_hallucinated_decisions(
        [{"candidate_key": "dblp:367/3657;;;62/5860-6;51/8073", "decision": "accept"}],
        {queue_key},
    )
    assert cleaned == [{"candidate_key": queue_key, "decision": "accept"}]
    assert not lint


def test_issues_summary_aggregates():
    issues = [
        LintIssue("critical", "a", "x"),
        LintIssue("warning", "b", "y"),
    ]
    s = issues_summary(issues)
    assert s["critical"] == 1
    assert s["warnings"] == 1


def test_format_issues_for_prompt_renders():
    text = format_issues_for_prompt([LintIssue("critical", "no_strong_terms", "x")])
    assert "[critical]" in text
    assert "no_strong_terms" in text


def test_lint_ignores_seed_overlap_in_brain_plan():
    plan = _good_plan()
    plan["judge_examples"]["out_of_scope"] = [
        {"title": "PromptAgent: Strategic Planning", "reason": "not SLM-specific"},
        {"title": "out2", "reason": "r"},
        {"title": "out3", "reason": "r"},
    ]
    plan["seed_papers"] = [
        {"title": "PromptAgent: Strategic Planning", "paper_role": "core_method", "required": True},
        {"title": "real-seed-2", "paper_role": "core_method", "required": True},
        {"title": "real-seed-3", "paper_role": "core_method", "required": True},
    ]
    issues = lint_plan_output(plan)
    assert not _has(issues, "seed_overlaps_out_of_scope")


def test_lint_allows_background_anchor_as_out_of_scope_contrast():
    plan = _good_plan()
    plan["judge_examples"]["out_of_scope"] = [
        {"title": "Chain-of-Thought Prompting", "reason": "explicit CoT contrast"},
        {"title": "out2", "reason": "r"},
        {"title": "out3", "reason": "r"},
    ]
    plan["seed_papers"] = [
        {"title": "Chain-of-Thought Prompting", "paper_role": "background_anchor", "required": True},
        {"title": "real-seed-2", "paper_role": "core_method", "required": True},
        {"title": "real-seed-3", "paper_role": "core_method", "required": True},
    ]

    issues = lint_plan_output(plan)

    assert not _has(issues, "seed_overlaps_out_of_scope")


def test_lint_ignores_seed_contrast_reason_in_brain_plan():
    plan = _good_plan()
    plan["seed_papers"] = [
        {"title": "PromptAgent",
         "paper_role": "core_method", "required": True,
         "why_seed": "Agent framework, but not SLM-specific; useful as contrast."},
        {"title": "TaskMatrix",
         "paper_role": "core_method", "required": True,
         "why_seed": "Task-oriented agent, relevant but not SLM-focused."},
        {"title": "real-seed-3", "paper_role": "core_method", "required": True},
    ]
    issues = lint_plan_output(plan)
    assert not _has(issues, "seed_reason_says_out_of_scope")


def test_repair_plan_seed_contract_downgrades_contradictory_required_core_seed():
    plan = _good_plan()
    plan["judge_examples"]["out_of_scope"] = [
        {"title": "PromptAgent: Strategic Planning", "reason": "not SLM-specific"},
        {"title": "out2", "reason": "r"},
        {"title": "out3", "reason": "r"},
    ]
    plan["seed_papers"] = [
        {"title": "PromptAgent: Strategic Planning", "paper_role": "core_method", "required": True},
        {"title": "real-seed-2", "paper_role": "core_method", "required": True},
        {"title": "real-seed-3", "paper_role": "core_method", "required": True},
    ]

    issues = repair_plan_seed_contract(plan)

    assert _has(issues, "seed_auto_downgraded_to_boundary_negative")
    assert plan["seed_papers"][0]["paper_role"] == "boundary_negative"
    assert plan["seed_papers"][0]["required"] is False


def test_lint_clean_seeds_pass():
    plan = _good_plan()
    plan["seed_papers"] = [
        {"title": "TinyAgent: Function Calling at the Edge",
         "paper_role": "core_method", "required": True,
         "why_seed": "Core SLM agent paper."},
        {"title": "Octopus v2",
         "paper_role": "core_method", "required": True,
         "why_seed": "On-device SLM agent."},
        {"title": "TinyLlama-based Agent",
         "paper_role": "core_method", "required": True,
         "why_seed": "Tiny model agent."},
    ]
    issues = lint_plan_output(plan)
    assert not _has(issues, "seed_overlaps_out_of_scope")
    assert not _has(issues, "seed_reason_says_out_of_scope")
