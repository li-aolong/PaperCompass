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
