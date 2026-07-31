"""Security regression tests for chat-record ownership.

AUDIT D-03 and D-04: two endpoints loaded a ChatRecord by primary key alone, so
any authenticated user could name another user's record id and receive an LLM
analysis of their data (D-03) or their question text (D-04).

Every sibling read path in apps/chat/curd/chat.py already carried a
``ChatRecord.create_by == current_user.id`` predicate (get_chart_data_with_user,
get_chat_predict_data_with_user, ...). These tests pin that predicate onto the
two paths that lacked it.

The assertions run against an in-memory SQLite database, so they are
deterministic and need no running server, no Postgres and no model.

Run in-container:
    docker cp tests sqlbot:/tmp/roottests
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \
        .venv/bin/python -m pytest /tmp/roottests/test_authz_chat_records.py -q"
"""

import pytest
from sqlmodel import Session, create_engine

from apps.chat.curd.chat import (get_analysis_base_record_with_user,
                                 get_chat_record_by_id,
                                 get_chat_record_by_id_with_user)
from apps.chat.models.chat_model import ChatRecord

OWNER_ID = 10
OTHER_ID = 20
OWNED_RECORD_ID = 1
OTHER_RECORD_ID = 2


class _User:
    """Minimal stand-in for UserInfoDTO — the CRUD functions only read ``.id``."""

    def __init__(self, user_id: int):
        self.id = user_id


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    ChatRecord.__table__.create(engine)
    with Session(engine) as s:
        s.add(ChatRecord(id=OWNED_RECORD_ID, chat_id=1, create_by=OWNER_ID,
                         question="owner question", chart='{"type":"table"}',
                         data='{"fields":[],"data":[]}'))
        s.add(ChatRecord(id=OTHER_RECORD_ID, chat_id=2, create_by=OTHER_ID,
                         question="someone else's question",
                         chart='{"type":"bar"}', data='{"fields":["secret"],"data":[]}'))
        s.commit()
        yield s


# --- D-03: /chat/record/{id}/{analysis|predict} ---------------------------

def test_analysis_base_record_returns_own_record(session):
    record = get_analysis_base_record_with_user(session, _User(OWNER_ID), OWNED_RECORD_ID)
    assert record is not None
    assert record.id == OWNED_RECORD_ID
    assert record.create_by == OWNER_ID
    # the fields analysis/predict actually consume must survive the scoping
    assert record.chart == '{"type":"table"}'
    assert record.data == '{"fields":[],"data":[]}'


def test_analysis_base_record_refuses_another_users_record(session):
    """D-03: the whole point. Another user's record must be indistinguishable
    from a missing one."""
    assert get_analysis_base_record_with_user(session, _User(OWNER_ID), OTHER_RECORD_ID) is None


def test_analysis_base_record_missing_id_returns_none(session):
    assert get_analysis_base_record_with_user(session, _User(OWNER_ID), 9999) is None


# --- D-04: /chat/recommend_questions/{id} ---------------------------------

def test_recommend_record_returns_own_record(session):
    record = get_chat_record_by_id_with_user(session, _User(OWNER_ID), OWNED_RECORD_ID)
    assert record is not None
    assert record.id == OWNED_RECORD_ID
    assert record.question == "owner question"


def test_recommend_record_refuses_another_users_record(session):
    """D-04: must not leak another user's question text or datasource."""
    assert get_chat_record_by_id_with_user(session, _User(OWNER_ID), OTHER_RECORD_ID) is None


def test_recommend_record_missing_id_returns_none(session):
    assert get_chat_record_by_id_with_user(session, _User(OWNER_ID), 9999) is None


# --- backward compatibility ------------------------------------------------

def test_unscoped_lookup_is_unchanged(session):
    """get_chat_record_by_id has 13 internal callers in curd/chat.py that are
    already scoped by their own callers. It must keep its existing unscoped
    behaviour so this fix stays zero-blast-radius."""
    record = get_chat_record_by_id(session, OTHER_RECORD_ID)
    assert record is not None
    assert record.create_by == OTHER_ID
