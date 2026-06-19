from papercompass.scope import (
    infer_publication_scope,
    paper_matches_publication_scope,
    publication_scope_from_topic,
    render_publication_scope,
)


def test_infer_publication_scope_from_chinese_top_venue_and_arxiv_direction() -> None:
    scope = infer_publication_scope(
        "调研 2023 年及以后的中文 GEC/CSC 顶会论文，以及比较新的 arXiv 论文",
        ["ACL Chinese grammatical error correction", "AAAI spelling correction"],
    )

    assert scope["strict"] is True
    assert scope["include_preprints"] is True
    assert "ACL" in scope["preferred_venues"]
    assert "AAAI" in scope["preferred_venues"]
    assert "Preferred venues" in render_publication_scope(scope)


def test_paper_matches_publication_scope_by_venue_or_arxiv() -> None:
    scope = {
        "policy": "preferred_venues_or_preprints",
        "strict": True,
        "preferred_venues": ["ACL", "EMNLP"],
        "include_preprints": True,
    }

    acl_paper = {"title": "A", "venue": "Proceedings of ACL 2025"}
    arxiv_paper = {"title": "B", "venue": "arXiv", "ids": {"arxiv": "2601.00001"}}
    journal_paper = {"title": "C", "venue": "Journal of Chinese Information Processing"}

    assert paper_matches_publication_scope(acl_paper, scope)[0] is True
    assert paper_matches_publication_scope(arxiv_paper, scope)[0] is True
    assert paper_matches_publication_scope(journal_paper, scope) == (False, "outside_publication_scope")


def test_acceptance_gate_drops_rejected_submission_with_matching_venue() -> None:
    # A rejected OpenReview submission carries venue=ICLR but is not accepted.
    scope = {
        "policy": "preferred_venues_or_preprints",
        "strict": True,
        "preferred_venues": ["ICLR", "NeurIPS"],
        "include_preprints": True,
    }
    rejected = {"title": "R", "venue": "ICLR", "publication_status": "rejected"}
    accepted = {"title": "A", "venue": "ICLR", "publication_status": "accepted"}
    # default acceptance = accepted_or_preprint
    assert paper_matches_publication_scope(rejected, scope) == (False, "not_accepted")
    assert paper_matches_publication_scope(accepted, scope)[0] is True
    # `any` restores legacy venue-only behavior
    assert paper_matches_publication_scope(rejected, {**scope, "acceptance": "any"})[0] is True


def test_acceptance_gate_keeps_rejected_submission_when_it_is_an_allowed_preprint() -> None:
    # "rejected at ICLR but on arXiv" — the user accepts recent arXiv versions.
    scope = {
        "policy": "preferred_venues_or_preprints",
        "strict": True,
        "preferred_venues": ["ICLR"],
        "include_preprints": True,
    }
    rejected_but_arxiv = {
        "title": "P",
        "venue": "ICLR",
        "publication_status": "rejected",
        "ids": {"arxiv": "2601.09999"},
    }
    # arXiv evidence is checked before the acceptance drop only when it is the
    # paper's venue; here venue=ICLR so the rejected status still drops it.
    assert paper_matches_publication_scope(rejected_but_arxiv, scope) == (False, "not_accepted")
    # If the same paper is surfaced as an arXiv preprint (venue=arXiv), keep it.
    arxiv_view = {**rejected_but_arxiv, "venue": "arXiv", "publication_status": "preprint"}
    assert paper_matches_publication_scope(arxiv_view, scope) == (True, "preprint_allowed")


def test_accepted_only_requires_acceptance_evidence_behind_venue_match() -> None:
    scope = {
        "policy": "preferred_venues_or_preprints",
        "strict": True,
        "preferred_venues": ["ICLR"],
        "include_preprints": True,
        "acceptance": "accepted_only",
    }
    unknown = {"title": "U", "venue": "ICLR"}  # no publication_status
    accepted = {"title": "A", "venue": "ICLR", "publication_status": "accepted"}
    assert paper_matches_publication_scope(unknown, scope) == (False, "not_confirmed_accepted")
    assert paper_matches_publication_scope(accepted, scope)[0] is True


def test_scope_expands_nlp_profile_when_venue_list_is_not_exhaustive() -> None:
    scope = infer_publication_scope(
        "Chinese grammatical error correction after 2023, covering ACL, EMNLP, NAACL, COLING, Findings and recent arXiv preprints",
    )

    assert scope["venue_profile"] == "nlp_top_ai"
    assert scope["strict_venue_list"] is False
    assert "AAAI" in scope["preferred_venues"]
    assert "ICLR" in scope["preferred_venues"]


def test_scope_keeps_exact_venue_list_when_user_says_only() -> None:
    scope = infer_publication_scope("仅限 ACL 和 EMNLP 的中文语法纠错论文")

    assert scope["strict_venue_list"] is True
    assert "ACL" in scope["preferred_venues"]
    assert "EMNLP" in scope["preferred_venues"]
    assert "AAAI" not in scope["preferred_venues"]


def test_configured_scope_supports_profile_include_and_exclude() -> None:
    scope = publication_scope_from_topic({
        "direction_raw": "Chinese grammatical error correction",
        "publication_scope": {
            "policy": "preferred_venues_or_preprints",
            "strict": True,
            "venue_profile": "nlp_top_ai",
            "include_venues": ["ICASSP"],
            "exclude_venues": ["ICLR"],
        },
    })

    assert "AAAI" in scope["preferred_venues"]
    assert "ICASSP" in scope["preferred_venues"]
    assert "ICLR" not in scope["preferred_venues"]
    assert paper_matches_publication_scope({"title": "X", "venue": "ICLR"}, scope) == (False, "excluded_venue")
