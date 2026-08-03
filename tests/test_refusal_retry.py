"""Tests for refusal-retry + output cap (issues found on the qwen32b BIRD run)."""
import os, sys
_roots = [os.environ.get('SQLBOT_APP_ROOT'),
          os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'backend'),
          '/opt/sqlbot/app']
for _r in _roots:
    if _r and os.path.isdir(os.path.join(_r, 'apps')):
        sys.path.insert(0, _r); break

from apps.chat.task.agentic import (REFUSAL_TAG, is_refusal_message, strip_refusal_tag,
                                    refusal_retry_feedback, is_retryable_single_message)

def test_refusal_is_detected_and_retryable():
    msg = f"{REFUSAL_TAG}the hint says MAX(dob) but youngest means MIN(dob)"
    assert is_refusal_message(msg)
    assert is_retryable_single_message(msg)

def test_plain_parse_errors_still_retryable():
    assert is_retryable_single_message('SQL answer is not a valid json object')
    assert is_retryable_single_message('Cannot parse sql from answer')
    assert is_retryable_single_message('SQL query is empty')

def test_unrelated_error_not_retryable():
    assert not is_retryable_single_message('Connect DB failed')
    assert not is_retryable_single_message('')

def test_tag_is_stripped_for_the_user():
    reason = "no fundraiser column exists"
    assert strip_refusal_tag(f'{REFUSAL_TAG}{reason}') == reason
    assert REFUSAL_TAG not in strip_refusal_tag(f'{REFUSAL_TAG}{reason}')

def test_feedback_contains_the_objection_and_no_tag():
    fb = refusal_retry_feedback(f'{REFUSAL_TAG}the hint is wrong')
    assert 'the hint is wrong' in fb
    assert REFUSAL_TAG not in fb
    assert 'Do not refuse again' in fb

def test_output_cap_applied_and_overridable():
    from apps.ai_model.model_factory import _with_output_cap
    from common.core.config import settings
    out = _with_output_cap({'temperature': 0})
    assert out['max_tokens'] == settings.LLM_MAX_OUTPUT_TOKENS
    assert out['temperature'] == 0
    # an explicit per-model setting must win
    assert _with_output_cap({'max_tokens': 42})['max_tokens'] == 42
    # and the original dict must not be mutated
    src = {'temperature': 0}; _with_output_cap(src)
    assert 'max_tokens' not in src
