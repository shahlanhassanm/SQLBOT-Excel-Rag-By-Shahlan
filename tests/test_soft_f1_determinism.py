"""Soft-F1 must not depend on the order rows come back from the database.

AUDIT D-10. `calculate_f1_score` matches `predicted[i]` against
`ground_truth[i]` POSITIONALLY. Most BIRD gold queries have no ORDER BY, so
PostgreSQL is free to return rows in any order and the positional pairing
shifts between runs.

Measured before the fix — the same committed results file re-scored three times
against an unchanged database:

    bird_final_32b.json   Soft-F1 = 51.8 / 52.0 / 52.5   (documented 52.7)

EX and EX-tolerant were stable across all three, because both are set
comparisons. Only the positional metric moved.

This is a metric defect, not a model defect: it makes any Soft-F1 delta below
about 1pp unreadable, and the number is quoted to one decimal place.

Run in-container:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \\
        .venv/bin/python -m pytest /tmp/roottests/test_soft_f1_determinism.py -q"
"""

import importlib.util
import os
import random

import pytest


def _load_bird_eval():
    """Import bird_eval.py by path — it lives in backend/tests/, which is not a
    package, and it is also copied to /tmp by the documented workflow."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        os.path.join(os.path.dirname(here), "backend", "tests", "bird_eval.py"),
        "/opt/sqlbot/app/tests/bird_eval.py",
        "/tmp/bird_eval.py",
    ):
        if os.path.exists(candidate):
            spec = importlib.util.spec_from_file_location("bird_eval_under_test", candidate)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    pytest.skip("bird_eval.py not found")


bird = _load_bird_eval()
f1 = bird.calculate_f1_score


# --- the defect --------------------------------------------------------------

def test_f1_is_invariant_to_predicted_row_order():
    """The core property. Same multiset of rows, different order, same score."""
    gold = [("alice", 10), ("bob", 20), ("carol", 30)]
    pred = [("alice", 10), ("bob", 20), ("carol", 30)]
    baseline = f1(pred, gold)
    for _ in range(50):
        shuffled = pred[:]
        random.shuffle(shuffled)
        assert f1(shuffled, gold) == baseline, (
            f"Soft-F1 moved with row order: {f1(shuffled, gold)} != {baseline}")


def test_f1_is_invariant_to_gold_row_order():
    gold = [("alice", 10), ("bob", 20), ("carol", 30)]
    pred = [("bob", 20), ("carol", 30), ("alice", 10)]
    baseline = f1(pred, gold)
    for _ in range(50):
        shuffled = gold[:]
        random.shuffle(shuffled)
        assert f1(pred, shuffled) == baseline


def test_reversed_order_scores_the_same_as_identical_order():
    """The worst case for positional matching: a perfect answer, reversed."""
    gold = [(i, f"v{i}") for i in range(8)]
    assert f1(list(reversed(gold)), gold) == f1(gold, gold) == 1.0


def test_partial_overlap_is_order_invariant():
    gold = [("a", 1), ("b", 2), ("c", 3), ("d", 4)]
    pred = [("c", 3), ("a", 1), ("z", 99)]
    baseline = f1(pred, gold)
    for _ in range(30):
        s = pred[:]
        random.shuffle(s)
        assert f1(s, gold) == baseline


# --- the sort key must survive real BIRD cell types --------------------------

def test_mixed_types_do_not_raise():
    """Rows carry None, str, int, float, Decimal and date. A naive sorted()
    raises TypeError on None vs str, so the key must be total-order safe."""
    import datetime
    import decimal
    gold = [
        (None, "x", 1),
        ("a", None, 2.5),
        ("b", "y", decimal.Decimal("3.10")),
        ("c", "z", datetime.date(2026, 1, 1)),
    ]
    pred = list(reversed(gold))
    assert f1(pred, gold) == f1(gold, gold)


def test_none_values_sort_consistently():
    gold = [(None,), ("a",), (None,), ("b",)]
    pred = [("b",), (None,), ("a",), (None,)]
    baseline = f1(pred, gold)
    for _ in range(20):
        s = pred[:]
        random.shuffle(s)
        assert f1(s, gold) == baseline


def test_rows_of_differing_width_do_not_raise():
    gold = [("a", 1), ("b",)]
    pred = [("b",), ("a", 1)]
    assert isinstance(f1(pred, gold), float)


# --- behaviour that must NOT change -----------------------------------------

def test_both_empty_is_one():
    assert f1([], []) == 1.0


def test_disjoint_results_score_zero():
    assert f1([("x", 1)], [("y", 2)]) == 0.0


def test_perfect_match_is_one():
    gold = [("a", 1), ("b", 2)]
    assert f1(gold, gold) == 1.0


def test_duplicate_rows_are_still_deduplicated():
    """dict.fromkeys dedup is pre-existing behaviour and must be preserved."""
    gold = [("a", 1)]
    assert f1([("a", 1), ("a", 1), ("a", 1)], gold) == 1.0


def test_empty_prediction_scores_zero():
    assert f1([], [("a", 1)]) == 0.0


# ===========================================================================
# GROUP A — benchmark provenance and metric consistency (E-06, E-08, E-09).
# ===========================================================================

def test_tolerant_f1_pairs_with_tolerant_ex():
    """E-09: a record could score ex_tol=1 and f1=0 at once, because EX-tolerant
    rounds floats to 6dp while official Soft-F1 uses exact membership. The
    tolerant pair must agree."""
    import decimal
    gold = [(decimal.Decimal("0.904908"),)]
    pred = [(0.9049079754601227,)]
    assert bird.calculate_ex_tolerant(pred, gold) == 1
    assert bird.calculate_f1_score(pred, gold) == 0.0        # official, unchanged
    assert bird.calculate_f1_tolerant(pred, gold) == 1.0     # tolerant pair


def test_official_f1_is_not_altered_by_the_tolerant_variant():
    """Comparability with published BIRD Soft-F1 must survive."""
    gold = [("a", 1), ("b", 2)]
    assert bird.calculate_f1_score(gold, gold) == 1.0
    assert bird.calculate_f1_score([("x", 9)], gold) == 0.0


def test_tolerant_f1_is_also_order_invariant():
    gold = [("a", 1), ("b", 2), ("c", 3)]
    assert bird.calculate_f1_tolerant(list(reversed(gold)), gold) == 1.0


def test_run_tag_is_stable_for_the_same_configuration():
    """E-08: the tag identifies a (model, mode, flags) combination."""
    import argparse
    a = argparse.Namespace(mode="pipeline", finish="data", descriptions=False, out="/x")
    b = argparse.Namespace(mode="pipeline", finish="data", descriptions=False, out="/y")
    p1 = bird.run_provenance(a, "m1")
    p2 = bird.run_provenance(b, "m1")
    assert bird.run_tag(p1) == bird.run_tag(p2), "--out must not affect the tag"


def test_run_tag_changes_when_a_flag_changes():
    import argparse
    a = argparse.Namespace(mode="pipeline", finish="sql", descriptions=False, out="/x")
    b = argparse.Namespace(mode="pipeline", finish="data", descriptions=False, out="/x")
    assert bird.run_tag(bird.run_provenance(a, "m")) != \
           bird.run_tag(bird.run_provenance(b, "m"))


def test_run_tag_changes_when_the_model_changes():
    import argparse
    a = argparse.Namespace(mode="pipeline", finish="data", out="/x")
    assert bird.run_tag(bird.run_provenance(a, "model-A")) != \
           bird.run_tag(bird.run_provenance(a, "model-B"))


def test_provenance_records_what_is_needed_to_reproduce():
    """E-06: results files carried no model, flags, commit or timestamp - which
    is how two byte-identical files ended up under different names (D-22)."""
    import argparse
    prov = bird.run_provenance(
        argparse.Namespace(mode="model", descriptions=True, out="/x"), "qwen")
    for key in ("model", "mode", "flags", "git_commit", "git_dirty", "bank", "utc"):
        assert key in prov, f"provenance missing {key}"
    assert prov["model"] == "qwen"
    assert "out" not in prov["flags"], "output path must not be part of identity"
