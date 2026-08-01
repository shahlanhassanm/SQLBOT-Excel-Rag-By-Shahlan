import concurrent
import json
import os
import re
import time
import traceback
import urllib.parse
import warnings
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime
from typing import Any, List, Optional, Union, Dict, Iterator

import orjson
import pandas as pd
import requests
import sqlparse
from langchain.chat_models.base import BaseChatModel
from langchain_community.utilities import SQLDatabase
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, BaseMessageChunk
from sqlalchemy import and_, select
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlbot_xpack.config.model import SysArgModel
from sqlbot_xpack.custom_prompt.curd.custom_prompt import find_custom_prompts
from sqlbot_xpack.custom_prompt.models.custom_prompt_model import CustomPromptTypeEnum
from sqlbot_xpack.license.license_manage import SQLBotLicenseUtil
from sqlmodel import Session

from apps.ai_model.model_factory import LLMConfig, LLMFactory, get_default_config
from apps.chat.curd.chat import save_question, save_sql_answer, save_sql, \
    save_error_message, save_sql_exec_data, save_chart_answer, save_chart, \
    finish_record, save_analysis_answer, save_predict_answer, save_predict_data, \
    save_select_datasource_answer, save_recommend_question_answer, \
    get_old_questions, save_analysis_predict_record, rename_chat, get_chart_config, \
    get_chat_chart_data, list_generate_sql_logs, list_generate_chart_logs, start_log, end_log, \
    get_last_execute_sql_error, format_json_data, format_chart_fields, get_chat_brief_generate, get_chat_predict_data, \
    get_chat_chart_config, trigger_log_error
from apps.chat.models.chat_model import ChatQuestion, ChatRecord, Chat, RenameChat, ChatLog, OperationEnum, \
    ChatFinishStep, AxisObj, SystemPromptMessage, HumanPromptMessage, AIPromptMessage
from apps.chat.task.agentic import (build_result_preview, format_retry_feedback,
                                    is_retryable_single_message, parse_grader_verdict,
                                    parse_decomposition, merge_union, raise_sql_limit,
                                    is_listing_question, has_explicit_row_count,
                                    result_fingerprint, select_by_consensus,
                                    REFUSAL_TAG as _REFUSAL_TAG, is_refusal_message,
                                    strip_refusal_tag, refusal_retry_feedback,
                                    apply_nulls_last)
from apps.chat.task.sql_validate import (format_identifier_feedback, sqlglot_dialect,
                                         validate_sql_identifiers)


def _csv_terms(value: str) -> list:
    """Parse a comma-separated settings string into a clean term list."""
    return [t.strip() for t in (value or '').split(',') if t.strip()]
from apps.data_training.curd.data_training import get_training_template
from apps.datasource.crud.datasource import get_table_schema, get_tables_sample_data
from apps.datasource.crud.permission import get_row_permission_filters, is_normal_user
from apps.datasource.embedding.ds_embedding import get_ds_embedding
from apps.datasource.models.datasource import CoreDatasource
from apps.datasource.value_index import build_value_hints
from apps.db.db import exec_sql, get_version, check_connection
from apps.system.crud.assistant import AssistantOutDs, AssistantOutDsFactory, get_assistant_ds
from apps.system.crud.parameter_manage import get_groups
from apps.system.schemas.system_schema import AssistantOutDsSchema
from apps.terminology.curd.terminology import get_terminology_template
from common.core.config import settings
from common.core.db import engine
from common.core.deps import CurrentAssistant, CurrentUser
from common.error import SingleMessageError, SQLBotDBError, ParseSQLResultError, SQLBotDBConnectionError
from common.utils.data_format import DataFormat
from common.utils.locale import I18n, I18nHelper
from common.utils.utils import SQLBotLogUtil, extract_nested_json, prepare_for_orjson

warnings.filterwarnings("ignore")

executor = ThreadPoolExecutor(max_workers=settings.LLM_EXECUTOR_MAX_WORKERS)


def current_prompt_time(tz_name: str | None = None) -> str:
    """The {current_time} prompt slot, in the configured timezone.

    The image pins Asia/Shanghai, so `datetime.now()` answered every temporal
    question in CST for every tenant (AUDIT H-17). An unset or unknown zone
    falls back to container-local time -- a typo must not take down question
    answering.
    """
    name = settings.PROMPT_TIMEZONE if tz_name is None else tz_name
    if name:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(name)).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            SQLBotLogUtil.warning(
                f"PROMPT_TIMEZONE={name!r} is not a known timezone; "
                f"falling back to container-local time")
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

dynamic_ds_types = [1, 3]
dynamic_subsql_prefix = 'select * from sqlbot_dynamic_temp_table_'

session_maker = scoped_session(sessionmaker(bind=engine, class_=Session))

i18n = I18n()


class _AgenticGraderReject(Exception):
    """Internal: the result grader judged an empty result as not answering the
    question. Carries the reason and the SQL so the retry prompt can use them."""

    def __init__(self, reason: str, sql: str):
        super().__init__(reason)
        self.reason = reason
        self.sql = sql


class _AgenticIdentifierReject(Exception):
    """Internal: the generated SQL references tables/columns that are not in the
    schema verbatim. Raised BEFORE execution so the retry prompt gets a precise
    'you wrote X, the schema says Y' hint instead of a driver error."""

    def __init__(self, reason: str, sql: str):
        super().__init__(reason)
        self.reason = reason
        self.sql = sql


class LLMService:
    ds: CoreDatasource
    chat_question: ChatQuestion
    record: ChatRecord
    config: LLMConfig
    llm: BaseChatModel
    sql_message: List[Union[BaseMessage, dict[str, Any]]]
    chart_message: List[Union[BaseMessage, dict[str, Any]]]

    # session: Session = db_session
    current_user: CurrentUser
    current_assistant: Optional[CurrentAssistant] = None
    out_ds_instance: Optional[AssistantOutDs] = None
    change_title: bool = False

    generate_sql_logs: List[ChatLog]
    generate_chart_logs: List[ChatLog]
    current_logs: dict[OperationEnum, ChatLog]
    chunk_list: List[str]
    future: Future

    trans: I18nHelper = None

    last_execute_sql_error: str = None
    articles_number: int = 4

    enable_sql_row_limit: bool = settings.GENERATE_SQL_QUERY_LIMIT_ENABLED
    base_message_round_count_limit: int = settings.GENERATE_SQL_QUERY_HISTORY_ROUND_COUNT

    def __init__(self, session: Session, current_user: CurrentUser, chat_question: ChatQuestion,
                 current_assistant: Optional[CurrentAssistant] = None, no_reasoning: bool = False,
                 embedding: bool = False, config: LLMConfig = None):
        self.sql_message = []
        self.chart_message = []
        self.generate_sql_logs = []
        self.generate_chart_logs = []
        self.current_logs = {}
        self.chunk_list = []
        self.current_user = current_user
        self.current_assistant = current_assistant
        # --- agentic pipeline state ---
        self.ds_candidates: list[dict] = []  # ranked alternatives (excludes chosen ds)
        self._ranked_candidates: list[dict] = []  # full ranked list incl. chosen ds
        self.ds_auto_selected: bool = False  # True only when the LLM picked the ds
        self._secondary_subs: list[dict] = []  # cross-ds sub-questions (decomposition)
        self._multi_mode: str = 'single'  # 'single' | 'fanout' | 'split'
        self._fanout_union: bool = False  # result was replaced by a fanout union table
        self._original_question: str = chat_question.question
        self._auto_route_chat: bool = False  # chat has no bound ds: re-route every question
        chat_id = chat_question.chat_id
        chat: Chat | None = session.get(Chat, chat_id)
        if not chat:
            raise SingleMessageError(f"Chat with id {chat_id} not found")
        ds: CoreDatasource | AssistantOutDsSchema | None = None
        if not chat.datasource and chat_question.datasource_id:
            _ds = session.get(CoreDatasource, chat_question.datasource_id)
            if _ds:
                if _ds.oid != current_user.oid:
                    raise SingleMessageError(
                        f"Datasource with id {chat_question.datasource_id} does not belong to current workspace")
                chat.datasource = _ds.id
                chat.engine_type = _ds.type_name
                # save chat
                session.add(chat)
                session.flush()
                session.refresh(chat)
                session.commit()

        if chat.datasource:
            # Get available datasource
            if current_assistant and current_assistant.type in dynamic_ds_types:
                self.out_ds_instance = AssistantOutDsFactory.get_instance(current_assistant)
                ds = self.out_ds_instance.get_ds(chat.datasource)
                if not ds:
                    raise SingleMessageError("No available datasource configuration found")
                chat_question.engine = ds.type + get_version(ds)
            else:
                ds = session.get(CoreDatasource, chat.datasource)
                if not ds:
                    raise SingleMessageError("No available datasource configuration found")
                chat_question.engine = (ds.type_name if ds.type != 'excel' else 'PostgreSQL') + get_version(ds)

        self.generate_sql_logs = list_generate_sql_logs(session=session, chart_id=chat_id)
        self.generate_chart_logs = list_generate_chart_logs(session=session, chart_id=chat_id)

        self.change_title = not get_chat_brief_generate(session=session, chat_id=chat_id)

        chat_question.lang = get_lang_name(current_user.language)
        self.trans = i18n(lang=current_user.language)

        self.ds = (
            ds if isinstance(ds, AssistantOutDsSchema) else CoreDatasource(**ds.model_dump())) if ds else None
        # Auto-route mode: the chat has no bound datasource (created via the
        # "Auto" option or MCP without ds), so the agentic finder must pick a
        # datasource for EVERY question. _apply_datasource therefore must not
        # persist its per-question choice onto the chat.
        self._auto_route_chat = self.ds is None and not current_assistant
        self.chat_question = chat_question
        self.config = config
        if no_reasoning:
            # only work while using qwen
            if self.config.additional_params:
                if self.config.additional_params.get('extra_body'):
                    if self.config.additional_params.get('extra_body').get('enable_thinking'):
                        del self.config.additional_params['extra_body']['enable_thinking']

        self.chat_question.ai_modal_id = self.config.model_id
        self.chat_question.ai_modal_name = self.config.model_name

        # Create LLM instance through factory
        llm_instance = LLMFactory.create_llm(self.config)
        self.llm = llm_instance.llm

        # get last_execute_sql_error
        last_execute_sql_error = get_last_execute_sql_error(session, self.chat_question.chat_id)
        if last_execute_sql_error:
            self.chat_question.error_msg = f'''<error-msg>
{last_execute_sql_error}
</error-msg>'''
        else:
            self.chat_question.error_msg = ''

    @classmethod
    async def create(cls, *args, **kwargs):
        specialized_model_id = None
        if args[3]:
            if args[3].enable_custom_model:
                if args[3].custom_model:
                    specialized_model_id = args[3].custom_model
                    print("use custom model: id[" + args[3].custom_model + "]")
        config: LLMConfig = await get_default_config(specialized_model_id)
        instance = cls(*args, **kwargs, config=config)

        chat_params: list[SysArgModel] = await get_groups(args[0], "chat")
        for config in chat_params:
            if config.pkey == 'chat.sqlbot_name':
                if config.pval.strip():
                    instance.chat_question.sqlbot_name = config.pval
            if config.pkey == 'chat.limit_rows':
                if config.pval.lower().strip() == 'true':
                    instance.enable_sql_row_limit = True
                else:
                    instance.enable_sql_row_limit = False
            if config.pkey == 'chat.context_record_count':
                count_value = config.pval
                if count_value is None:
                    count_value = settings.GENERATE_SQL_QUERY_HISTORY_ROUND_COUNT
                count_value = int(count_value)
                if count_value < 0:
                    count_value = 0
                instance.base_message_round_count_limit = count_value
        return instance

    def is_running(self, timeout=0.5):
        try:
            r = concurrent.futures.wait([self.future], timeout)
            if len(r.not_done) > 0:
                return True
            else:
                return False
        except Exception as e:
            return True

    def init_messages(self, session: Session):

        self.choose_table_schema(session)

        # APEX-SQL refinement: schema-agnostic plan -> dual-pathway prune -> data profiling.
        # Each step is graceful on failure and falls back to the prior state, so any
        # error here cannot break SQL generation downstream.
        # Skipped for agentic secondary legs (cross-ds sub-questions), which trade
        # the refinement quality for bounded latency.
        if settings.APEX_ENABLED and not getattr(self, '_skip_apex_refinement', False):
            self.generate_logical_plan(session)
            self.prune_schema_dual_pathway(session)
            self.profile_data_parallel(session)

        last_sql_messages: List[dict[str, Any]] = self.generate_sql_logs[-1].messages if len(
            self.generate_sql_logs) > 0 else []
        if self.chat_question.regenerate_record_id:
            # filter record before regenerate_record_id
            _temp_log = next(
                filter(lambda obj: obj.pid == self.chat_question.regenerate_record_id, self.generate_sql_logs), None)
            last_sql_messages: List[dict[str, Any]] = _temp_log.messages if _temp_log else []

        # 排除所有的系统提示词
        last_sql_messages = [obj for obj in last_sql_messages if obj.get("sqlbot_system") != True]

        count_limit = self.base_message_round_count_limit

        self.sql_message = []
        # add sys prompt
        _system_templates = self.chat_question.sql_sys_question(self.ds.type, self.enable_sql_row_limit)
        from apps.chat.task.apex_helpers import fts_prompt_addendum, parallel_columns_addendum
        _fts = fts_prompt_addendum(self.ds.type, settings.EXCEL_FTS_ENABLED)
        if _fts:
            _system_templates['rules'] = (_system_templates.get('rules') or '') + _fts
        # Guide the model to UNION across parallel/repeated columns (the "rest of the
        # answer is in another column" case) using the actual loaded schema — dynamic
        # per document, fires only when repeated columns are present.
        _parallel = parallel_columns_addendum(
            self.chat_question.db_schema, settings.PARALLEL_COLUMNS_HINT_ENABLED)
        if _parallel:
            _system_templates['rules'] = (_system_templates.get('rules') or '') + _parallel
        # The scripted AI acknowledgments follow the user's language: injecting
        # Chinese turns into an otherwise-English conversation primes the model
        # toward Chinese and reads as noise to non-Chinese models.
        self.sql_message.append(SystemPromptMessage(content=_system_templates['system']))
        self.sql_message.append(HumanPromptMessage(content=_system_templates['rules']))
        self.sql_message.append(
            AIPromptMessage(content=self.trans('i18n_chat.ack_rules')))
        self.sql_message.append(HumanPromptMessage(content=_system_templates['schema']))
        self.sql_message.append(
            AIPromptMessage(content=self.trans('i18n_chat.ack_schema')))
        if _system_templates.get('custom_prompt'):
            self.sql_message.append(HumanPromptMessage(content=_system_templates['custom_prompt']))
            self.sql_message.append(AIPromptMessage(content=self.trans('i18n_chat.ack_extra')))
        if _system_templates.get('terminologies'):
            self.sql_message.append(HumanPromptMessage(content=_system_templates['terminologies']))
            self.sql_message.append(AIPromptMessage(content=self.trans('i18n_chat.ack_terminology')))
        if _system_templates.get('data_training'):
            self.sql_message.append(HumanPromptMessage(content=_system_templates['data_training']))
            self.sql_message.append(AIPromptMessage(content=self.trans('i18n_chat.ack_training')))

        # Cross-datasource secondary legs (fanout/split) must NOT inherit prior
        # conversation history: that history contains SQL written against a
        # DIFFERENT datasource's tables (e.g. the primary leg's table), and the
        # model copies it verbatim, ignoring this leg's own schema. Each leg is an
        # independent sub-question on its own datasource, so history is skipped.
        if last_sql_messages is not None and len(last_sql_messages) > 0 \
                and not getattr(self, '_skip_leg_history', False):
            last_rounds = get_last_conversation_rounds(last_sql_messages, rounds=count_limit)

            for _msg_dict in last_rounds:
                _msg: BaseMessage
                if _msg_dict.get('type') == 'human':
                    _msg = HumanMessage(content=_msg_dict.get('content'))
                    self.sql_message.append(_msg)
                elif _msg_dict.get('type') == 'ai':
                    _msg = AIMessage(content=_msg_dict.get('content'))
                    self.sql_message.append(_msg)

        last_chart_messages: List[dict[str, Any]] = self.generate_chart_logs[-1].messages if len(
            self.generate_chart_logs) > 0 else []
        if self.chat_question.regenerate_record_id:
            # filter record before regenerate_record_id
            _temp_log = next(
                filter(lambda obj: obj.pid == self.chat_question.regenerate_record_id, self.generate_chart_logs), None)
            last_chart_messages: List[dict[str, Any]] = _temp_log.messages if _temp_log else []

        # 排除所有的系统提示词
        last_chart_messages = [obj for obj in last_chart_messages if obj.get("sqlbot_system") != True]

        count_chart_limit = self.base_message_round_count_limit

        self.chart_message = []
        # add sys prompt
        _chart_system_templates = self.chat_question.chart_sys_question()
        self.chart_message.append(SystemPromptMessage(content=_chart_system_templates['system']))
        self.chart_message.append(HumanPromptMessage(content=_chart_system_templates['rules']))
        self.chart_message.append(AIPromptMessage(content=self.trans('i18n_chat.ack_chart_rules')))
        if last_chart_messages is not None and len(last_chart_messages) > 0:
            last_rounds = get_last_conversation_rounds(last_chart_messages, rounds=count_chart_limit)

            for _msg_dict in last_rounds:
                _msg: BaseMessage
                if _msg_dict.get('type') == 'human':
                    _msg = HumanMessage(content=_msg_dict.get('content'))
                    self.chart_message.append(_msg)
                elif _msg_dict.get('type') == 'ai':
                    _msg = AIMessage(content=_msg_dict.get('content'))
                    self.chart_message.append(_msg)

    def init_record(self, session: Session) -> ChatRecord:
        self.record = save_question(session=session, current_user=self.current_user, question=self.chat_question)
        return self.record

    def get_record(self):
        return self.record

    def set_record(self, record: ChatRecord):
        self.record = record

    def set_articles_number(self, articles_number: int):
        self.articles_number = articles_number

    def get_fields_from_chart(self, _session: Session):
        chart_info = get_chart_config(_session, self.record.id)
        return format_chart_fields(chart_info)

    def filter_terminology_template(self, _session: Session, oid: int = None, ds_id: int = None):
        calculate_oid = oid
        calculate_ds_id = ds_id
        if self.current_assistant:
            calculate_oid = self.current_assistant.oid if self.current_assistant.type != 4 else self.current_user.oid
            if self.current_assistant.type == 1:
                calculate_ds_id = None
        self.current_logs[OperationEnum.FILTER_TERMS] = start_log(session=_session,
                                                                  operate=OperationEnum.FILTER_TERMS,
                                                                  record_id=self.record.id, local_operation=True)

        self.chat_question.terminologies, term_list = get_terminology_template(_session, self.chat_question.question,
                                                                               calculate_oid, calculate_ds_id)
        self.current_logs[OperationEnum.FILTER_TERMS] = end_log(session=_session,
                                                                log=self.current_logs[OperationEnum.FILTER_TERMS],
                                                                full_message=term_list)

    def filter_custom_prompts(self, _session: Session, custom_prompt_type: CustomPromptTypeEnum, oid: int = None,
                              ds_id: int = None):
        if SQLBotLicenseUtil.valid():
            calculate_oid = oid
            calculate_ds_id = ds_id
            if self.current_assistant:
                calculate_oid = self.current_assistant.oid if self.current_assistant.type != 4 else self.current_user.oid
                if self.current_assistant.type == 1:
                    calculate_ds_id = None
            self.current_logs[OperationEnum.FILTER_CUSTOM_PROMPT] = start_log(session=_session,
                                                                              operate=OperationEnum.FILTER_CUSTOM_PROMPT,
                                                                              record_id=self.record.id,
                                                                              local_operation=True)
            self.chat_question.custom_prompt, prompt_list = find_custom_prompts(_session, custom_prompt_type,
                                                                                calculate_oid,
                                                                                calculate_ds_id)
            self.current_logs[OperationEnum.FILTER_CUSTOM_PROMPT] = end_log(session=_session,
                                                                            log=self.current_logs[
                                                                                OperationEnum.FILTER_CUSTOM_PROMPT],
                                                                            full_message=prompt_list)

    def filter_training_template(self, _session: Session, oid: int = None, ds_id: int = None):
        self.current_logs[OperationEnum.FILTER_SQL_EXAMPLE] = start_log(session=_session,
                                                                        operate=OperationEnum.FILTER_SQL_EXAMPLE,
                                                                        record_id=self.record.id,
                                                                        local_operation=True)
        calculate_oid = oid
        calculate_ds_id = ds_id
        if self.current_assistant:
            calculate_oid = self.current_assistant.oid if self.current_assistant.type != 4 else self.current_user.oid
            if self.current_assistant.type == 1:
                calculate_ds_id = None
        if self.current_assistant and self.current_assistant.type == 1:
            self.chat_question.data_training, example_list = get_training_template(_session,
                                                                                   self.chat_question.question,
                                                                                   calculate_oid,
                                                                                   None, self.current_assistant.id)
        else:
            self.chat_question.data_training, example_list = get_training_template(_session,
                                                                                   self.chat_question.question,
                                                                                   calculate_oid,
                                                                                   calculate_ds_id)
        self.current_logs[OperationEnum.FILTER_SQL_EXAMPLE] = end_log(session=_session,
                                                                      log=self.current_logs[
                                                                          OperationEnum.FILTER_SQL_EXAMPLE],
                                                                      full_message=example_list)

    def choose_table_schema(self, _session: Session):
        self.current_logs[OperationEnum.CHOOSE_TABLE] = start_log(session=_session,
                                                                  operate=OperationEnum.CHOOSE_TABLE,
                                                                  record_id=self.record.id,
                                                                  local_operation=True)
        self.chat_question.db_schema, tables = self.out_ds_instance.get_db_schema(
            self.ds.id, self.chat_question.question) if self.out_ds_instance else get_table_schema(
            session=_session,
            current_user=self.current_user,
            ds=self.ds,
            question=self.chat_question.question)

        # Get sample data for all tables
        if not self.out_ds_instance:
            self.chat_question.sample_data = get_tables_sample_data(
                session=_session,
                current_user=self.current_user,
                ds=self.ds,
                table_list=tables)

            # Value linking (pre-generation cell retrieval): resolve the literals
            # in the question to their real cell values so the model writes
            # WHERE status = 'Paid' rather than guessing the spelling. Appended
            # to sample_data because that slot already carries observed values
            # into the prompt; returns '' (and issues no query) when the question
            # contains no literal to resolve.
            _value_hints = build_value_hints(
                session=_session,
                current_user=self.current_user,
                ds=self.ds,
                table_names=tables,
                question=self.chat_question.question)
            if _value_hints:
                self.chat_question.sample_data = (
                    (self.chat_question.sample_data or '') + '\n' + _value_hints)

        self.current_logs[OperationEnum.CHOOSE_TABLE] = end_log(session=_session,
                                                                log=self.current_logs[OperationEnum.CHOOSE_TABLE],
                                                                full_message=self.chat_question.db_schema)

    def generate_analysis(self, _session: Session):
        fields = self.get_fields_from_chart(_session)
        self.chat_question.fields = orjson.dumps(fields).decode()
        data = get_chat_chart_data(_session, self.record.id)
        self.chat_question.data = orjson.dumps(data.get('data')).decode()
        analysis_msg: List[Union[BaseMessage, dict[str, Any]]] = []

        ds_id = self.ds.id if isinstance(self.ds, CoreDatasource) else None

        self.filter_terminology_template(_session, self.current_user.oid, ds_id)

        self.filter_custom_prompts(_session, CustomPromptTypeEnum.ANALYSIS, self.current_user.oid, ds_id)

        analysis_msg.append(SystemPromptMessage(content=self.chat_question.analysis_sys_question()))
        analysis_msg.append(HumanMessage(content=self.chat_question.analysis_user_question()))

        self.current_logs[OperationEnum.ANALYSIS] = start_log(session=_session,
                                                              ai_modal_id=self.chat_question.ai_modal_id,
                                                              ai_modal_name=self.chat_question.ai_modal_name,
                                                              operate=OperationEnum.ANALYSIS,
                                                              record_id=self.record.id,
                                                              full_message=[
                                                                  {'type': msg.type,
                                                                   'sqlbot_system': getattr(msg, 'sqlbot_system',
                                                                                            False) is True,
                                                                   'content': msg.content} for
                                                                  msg
                                                                  in analysis_msg])
        full_thinking_text = ''
        full_analysis_text = ''
        token_usage = {}
        res = process_stream(self.llm.stream(analysis_msg), token_usage)
        for chunk in res:
            if chunk.get('content'):
                full_analysis_text += chunk.get('content')
            if chunk.get('reasoning_content'):
                full_thinking_text += chunk.get('reasoning_content')
            yield chunk

        analysis_msg.append(AIMessage(full_analysis_text))

        self.current_logs[OperationEnum.ANALYSIS] = end_log(session=_session,
                                                            log=self.current_logs[
                                                                OperationEnum.ANALYSIS],
                                                            full_message=[
                                                                {'type': msg.type,
                                                                 'sqlbot_system': getattr(msg, 'sqlbot_system',
                                                                                          False) is True,
                                                                 'content': msg.content}
                                                                for msg in analysis_msg],
                                                            reasoning_content=full_thinking_text,
                                                            token_usage=token_usage)
        self.record = save_analysis_answer(session=_session, record_id=self.record.id,
                                           answer=orjson.dumps({'content': full_analysis_text}).decode())

    def generate_predict(self, _session: Session):
        fields = self.get_fields_from_chart(_session)
        self.chat_question.fields = orjson.dumps(fields).decode()
        data = get_chat_chart_data(_session, self.record.id)
        self.chat_question.data = orjson.dumps(data.get('data')).decode()

        ds_id = self.ds.id if isinstance(self.ds, CoreDatasource) else None
        self.filter_custom_prompts(_session, CustomPromptTypeEnum.PREDICT_DATA, self.current_user.oid, ds_id)

        predict_msg: List[Union[BaseMessage, dict[str, Any]]] = []
        predict_msg.append(SystemPromptMessage(content=self.chat_question.predict_sys_question()))
        predict_msg.append(HumanMessage(content=self.chat_question.predict_user_question()))

        self.current_logs[OperationEnum.PREDICT_DATA] = start_log(session=_session,
                                                                  ai_modal_id=self.chat_question.ai_modal_id,
                                                                  ai_modal_name=self.chat_question.ai_modal_name,
                                                                  operate=OperationEnum.PREDICT_DATA,
                                                                  record_id=self.record.id,
                                                                  full_message=[
                                                                      {'type': msg.type,
                                                                       'sqlbot_system': getattr(msg, 'sqlbot_system',
                                                                                                False) is True,
                                                                       'content': msg.content} for
                                                                      msg
                                                                      in predict_msg])
        full_thinking_text = ''
        full_predict_text = ''
        token_usage = {}
        res = process_stream(self.llm.stream(predict_msg), token_usage)
        for chunk in res:
            if chunk.get('content'):
                full_predict_text += chunk.get('content')
            if chunk.get('reasoning_content'):
                full_thinking_text += chunk.get('reasoning_content')
            yield chunk

        predict_msg.append(AIMessage(full_predict_text))
        self.record = save_predict_answer(session=_session, record_id=self.record.id,
                                          answer=orjson.dumps({'content': full_predict_text}).decode())
        self.current_logs[OperationEnum.PREDICT_DATA] = end_log(session=_session,
                                                                log=self.current_logs[
                                                                    OperationEnum.PREDICT_DATA],
                                                                full_message=[
                                                                    {'type': msg.type,
                                                                     'sqlbot_system': getattr(msg, 'sqlbot_system',
                                                                                              False) is True,
                                                                     'content': msg.content}
                                                                    for msg in predict_msg],
                                                                reasoning_content=full_thinking_text,
                                                                token_usage=token_usage)

    def generate_recommend_questions_task(self, _session: Session):

        # get schema
        if self.ds and not self.chat_question.db_schema:
            self.chat_question.db_schema, tables = self.out_ds_instance.get_db_schema(
                self.ds.id, self.chat_question.question) if self.out_ds_instance else get_table_schema(
                session=_session,
                current_user=self.current_user, ds=self.ds,
                question=self.chat_question.question,
                embedding=False)

            # Get sample data for all tables
            # if not self.out_ds_instance:
            #     self.chat_question.sample_data = get_tables_sample_data(
            #         session=_session,
            #         current_user=self.current_user,
            #         ds=self.ds)

        guess_msg: List[Union[BaseMessage, dict[str, Any]]] = []
        guess_msg.append(SystemPromptMessage(content=self.chat_question.guess_sys_question(self.articles_number)))

        old_questions = list(map(lambda q: q.strip(), get_old_questions(_session, self.record.datasource)))
        guess_msg.append(
            HumanMessage(content=self.chat_question.guess_user_question(orjson.dumps(old_questions).decode())))

        self.current_logs[OperationEnum.GENERATE_RECOMMENDED_QUESTIONS] = start_log(session=_session,
                                                                                    ai_modal_id=self.chat_question.ai_modal_id,
                                                                                    ai_modal_name=self.chat_question.ai_modal_name,
                                                                                    operate=OperationEnum.GENERATE_RECOMMENDED_QUESTIONS,
                                                                                    record_id=self.record.id,
                                                                                    full_message=[
                                                                                        {'type': msg.type,
                                                                                         'sqlbot_system': getattr(msg,
                                                                                                                  'sqlbot_system',
                                                                                                                  False) is True,
                                                                                         'content': msg.content} for
                                                                                        msg
                                                                                        in guess_msg])
        full_thinking_text = ''
        full_guess_text = ''
        token_usage = {}
        res = process_stream(self.llm.stream(guess_msg), token_usage)
        for chunk in res:
            if chunk.get('content'):
                full_guess_text += chunk.get('content')
            if chunk.get('reasoning_content'):
                full_thinking_text += chunk.get('reasoning_content')
            yield chunk

        guess_msg.append(AIMessage(full_guess_text))

        self.current_logs[OperationEnum.GENERATE_RECOMMENDED_QUESTIONS] = end_log(session=_session,
                                                                                  log=self.current_logs[
                                                                                      OperationEnum.GENERATE_RECOMMENDED_QUESTIONS],
                                                                                  full_message=[
                                                                                      {'type': msg.type,
                                                                                       'sqlbot_system': getattr(msg,
                                                                                                                'sqlbot_system',
                                                                                                                False) is True,
                                                                                       'content': msg.content}
                                                                                      for msg in guess_msg],
                                                                                  reasoning_content=full_thinking_text,
                                                                                  token_usage=token_usage)
        self.record = save_recommend_question_answer(session=_session, record_id=self.record.id,
                                                     answer={'content': full_guess_text},
                                                     articles_number=self.articles_number)

        yield {'recommended_question': self.record.recommended_question}

    def select_datasource(self, _session: Session):
        datasource_msg: List[Union[BaseMessage, dict[str, Any]]] = []
        datasource_msg.append(SystemPromptMessage(self.chat_question.datasource_sys_question()))
        if self.current_assistant and self.current_assistant.type != 4:
            _ds_list = get_assistant_ds(session=_session, llm_service=self)
        else:
            stmt = select(CoreDatasource.id, CoreDatasource.name, CoreDatasource.description).where(
                and_(CoreDatasource.oid == self.current_user.oid))
            _ds_list = [
                {
                    "id": ds.id,
                    "name": ds.name,
                    "description": ds.description
                }
                for ds in _session.exec(stmt)
            ]
        if not _ds_list:
            raise SingleMessageError('No available datasource configuration found')
        ignore_auto_select = _ds_list and len(_ds_list) == 1
        # ignore auto select ds

        full_thinking_text = ''
        full_text = ''
        if not ignore_auto_select:
            if settings.TABLE_EMBEDDING_ENABLED and (
                    not self.current_assistant or (self.current_assistant and self.current_assistant.type != 1)):
                _ds_list = get_ds_embedding(_session, self.current_user, _ds_list, self.out_ds_instance,
                                            self.chat_question.question, self.current_assistant)
                # yield {'content': '{"id":' + str(ds.get('id')) + '}'}
            self.ds_auto_selected = True
            self._ranked_candidates = [dict(_d) for _d in _ds_list]

            _ds_list_dict = []
            for _ds in _ds_list:
                _ds_list_dict.append(_ds)
            datasource_msg.append(
                HumanMessage(self.chat_question.datasource_user_question(orjson.dumps(_ds_list_dict).decode())))

            self.current_logs[OperationEnum.CHOOSE_DATASOURCE] = start_log(session=_session,
                                                                           ai_modal_id=self.chat_question.ai_modal_id,
                                                                           ai_modal_name=self.chat_question.ai_modal_name,
                                                                           operate=OperationEnum.CHOOSE_DATASOURCE,
                                                                           record_id=self.record.id,
                                                                           full_message=[{'type': msg.type,
                                                                                          'sqlbot_system': getattr(msg,
                                                                                                                   'sqlbot_system',
                                                                                                                   False) is True,
                                                                                          'content': msg.content}
                                                                                         for
                                                                                         msg in datasource_msg])

            token_usage = {}
            res = process_stream(self.llm.stream(datasource_msg), token_usage)
            for chunk in res:
                if chunk.get('content'):
                    full_text += chunk.get('content')
                if chunk.get('reasoning_content'):
                    full_thinking_text += chunk.get('reasoning_content')
                yield chunk
            datasource_msg.append(AIMessage(full_text))

            self.current_logs[OperationEnum.CHOOSE_DATASOURCE] = end_log(session=_session,
                                                                         log=self.current_logs[
                                                                             OperationEnum.CHOOSE_DATASOURCE],
                                                                         full_message=[
                                                                             {'type': msg.type,
                                                                              'sqlbot_system': getattr(msg,
                                                                                                       'sqlbot_system',
                                                                                                       False) is True,
                                                                              'content': msg.content}
                                                                             for msg in datasource_msg],
                                                                         reasoning_content=full_thinking_text,
                                                                         token_usage=token_usage)

            json_str = extract_nested_json(full_text)
            if json_str is None:
                raise SingleMessageError(f'Cannot parse datasource from answer: {full_text}')
            ds = orjson.loads(json_str)

        _error: Exception | None = None
        _datasource: int | None = None
        _engine_type: str | None = None
        try:
            data: dict = _ds_list[0] if ignore_auto_select else ds

            if data.get('id') and data.get('id') != 0:
                _engine_type = self._apply_datasource(_session, data['id'])
                _datasource = data['id']
            elif data.get('fail'):
                # The finder matches on datasource name + description only, so a
                # question that doesn't reference those won't match. Raise a clear,
                # localised message instead of the prompt's hardcoded Chinese string.
                raise SingleMessageError(
                    'No matching datasource was found for your question. Make sure a '
                    'relevant datasource exists and that its name or description '
                    'reflects the data it contains.')
            else:
                raise SingleMessageError('No available datasource configuration found')

        except Exception as e:
            _error = e

        # keep the remaining ranked candidates for agentic datasource fallback
        if _datasource is not None and self._ranked_candidates:
            self.ds_candidates = [c for c in self._ranked_candidates if c.get('id') != _datasource]

        if not ignore_auto_select and not settings.TABLE_EMBEDDING_ENABLED:
            self.record = save_select_datasource_answer(session=_session, record_id=self.record.id,
                                                        answer=orjson.dumps({'content': full_text}).decode(),
                                                        datasource=_datasource,
                                                        engine_type=_engine_type)

        if _error:
            raise _error

    def _apply_datasource(self, _session: Session, ds_id: int) -> str:
        """Resolve ``ds_id``, set ``self.ds`` + engine, persist it on the chat and
        re-initialize the prompt context (terminology/training/custom prompts +
        init_messages). Shared by select_datasource, the agentic datasource
        fallback and cross-datasource secondary legs. Returns the engine type."""
        if self.current_assistant and self.current_assistant.type in dynamic_ds_types:
            _ds = self.out_ds_instance.get_ds(ds_id)
            self.ds = _ds
            self.chat_question.engine = _ds.type + get_version(self.ds)

            _engine_type = self.chat_question.engine
            _chat_engine_type = _ds.type
        else:
            _ds = _session.get(CoreDatasource, ds_id)
            if not _ds:
                raise SingleMessageError(f"Datasource configuration with id {ds_id} not found")
            self.ds = CoreDatasource(**_ds.model_dump())
            self.chat_question.engine = (_ds.type_name if _ds.type != 'excel' else 'PostgreSQL') + get_version(
                self.ds)

            _engine_type = self.chat_question.engine
            _chat_engine_type = _ds.type_name
        # save chat — but never bind auto-route chats to one datasource: they
        # must go through datasource selection again on every question
        if not self._auto_route_chat:
            _chat = _session.get(Chat, self.record.chat_id)
            _chat.datasource = ds_id
            _chat.engine_type = _chat_engine_type
            with _session.begin_nested():
                # 为了能继续记日志，先单独处理下事务
                try:
                    _session.add(_chat)
                    _session.flush()
                    _session.refresh(_chat)
                    _session.commit()
                except Exception as e:
                    _session.rollback()
                    raise e

        oid = self.ds.oid if isinstance(self.ds, CoreDatasource) else 1
        _ds_id_v = self.ds.id if isinstance(self.ds, CoreDatasource) else None

        self.filter_terminology_template(_session, oid, _ds_id_v)

        self.filter_training_template(_session, oid, _ds_id_v)

        self.filter_custom_prompts(_session, CustomPromptTypeEnum.GENERATE_SQL, oid, _ds_id_v)

        self.init_messages(_session)

        return _engine_type

    # ------------------------------------------------------------------
    # Agentic pipeline: result grading, multi-candidate generation,
    # cross-datasource decomposition and synthesis.
    # ------------------------------------------------------------------
    def validate_identifiers(self, sql: str) -> list:
        """Check the generated SQL's identifiers against the prompt's schema.

        Returns a (possibly empty) findings list; never raises. Uses the schema
        string actually sent to the model — including any APEX pruning — so the
        comparison is against exactly what the model was shown."""
        if not settings.AGENTIC_IDENTIFIER_CHECK_ENABLED:
            return []
        try:
            findings = validate_sql_identifiers(
                sql=sql,
                schema_str=self.chat_question.db_schema,
                ds_type=self.ds.type if self.ds else None,
                enabled=True)
            if findings:
                SQLBotLogUtil.info(
                    'identifier check rejected sql: ' + json.dumps(findings, ensure_ascii=False))
            return findings
        except Exception:
            # fail open: a validator bug must never block a good query
            traceback.print_exc()
            return []

    def grade_sql_result(self, _session: Session, sql: str, result: dict) -> tuple[bool, str]:
        """LLM-judge whether an (empty) execution result plausibly answers the
        question. Any internal failure degrades to acceptance — the grader must
        never be able to break the pipeline."""
        if not settings.AGENTIC_GRADER_ENABLED:
            return True, ''
        try:
            preview = build_result_preview(result.get('fields'), result.get('data'))
            msgs: List[Union[BaseMessage, dict[str, Any]]] = [
                SystemPromptMessage(self.chat_question.grader_sys_question()),
                HumanMessage(self.chat_question.grader_user_question(sql=sql, result_preview=preview))]
            text, _, _ = self._invoke_llm_blocking(msgs)
            verdict, reason = parse_grader_verdict(text)
            SQLBotLogUtil.info(f'agentic grader verdict: pass={verdict} reason={reason}')
            return verdict, reason
        except Exception:
            traceback.print_exc()
            return True, ''

    def _apply_nulls_last(self, sql: str) -> str:
        """Deterministic `ORDER BY x DESC` -> `... DESC NULLS LAST` (AUDIT L-A).

        PostgreSQL and Oracle sort NULL as larger than any value, so a
        superlative query returns a NULL row instead of the answer. Gated to the
        dialects that both need it and accept it, and fails open on any error --
        an unparseable statement is executed exactly as the model wrote it."""
        if not settings.AGENTIC_NULLS_LAST_ENABLED or not sql:
            return sql
        try:
            ds_type = (getattr(self.ds, 'type', None) or '').strip().lower()
            if ds_type not in _csv_terms(settings.AGENTIC_NULLS_LAST_DIALECTS):
                return sql
            rewritten = apply_nulls_last(sql, sqlglot_dialect(ds_type))
            if rewritten != sql:
                SQLBotLogUtil.info('nulls-last rewrite applied')
            return rewritten
        except Exception:
            traceback.print_exc()
            return sql

    def _maybe_raise_limit(self, sql: str) -> str:
        """Lift an over-small trailing LIMIT, but only for retrieval
        ("list/show all X") questions — the lift exists to complete listings.
        Applied unconditionally it turns a correct superlative answer
        (`ORDER BY x DESC LIMIT 1`) into a 1000-row table. An explicit row
        count in the question always wins over both paths."""
        if has_explicit_row_count(self._original_question,
                                  _csv_terms(settings.AGENTIC_ROW_COUNT_KEYWORDS)):
            return sql
        if not is_listing_question(self._original_question,
                                   _csv_terms(settings.AGENTIC_LISTING_KEYWORDS),
                                   _csv_terms(settings.AGENTIC_AGGREGATION_KEYWORDS)):
            return sql
        return raise_sql_limit(sql, settings.AGENTIC_FULL_RESULT_LIMIT)

    def _spawn_alt_candidate(self) -> Optional[Future]:
        """Kick off a blocking, structurally-different SQL candidate in parallel
        with the streamed regeneration (self-consistency: first working
        candidate wins). Returns a Future of (text, thinking, token_usage)."""
        if not settings.AGENTIC_MULTI_CANDIDATE_ENABLED:
            return None
        try:
            msgs = list(self.sql_message) + [HumanMessage(self.chat_question.alt_candidate_hint())]
            return executor.submit(self._invoke_llm_blocking, msgs)
        except Exception:
            traceback.print_exc()
            return None

    def _spawn_consistency_candidates(self) -> list:
        """Kick off the extra SQL candidates for execution-based voting.

        The messages are assembled here rather than reusing generate_sql because
        that is a generator — its question append happens on first iteration, so
        waiting for it would serialise the candidates behind the streamed one.
        Building the list up front lets all N run concurrently, which is the
        difference between +1 round-trip and +N round-trips of wall clock."""
        if not settings.AGENTIC_SELF_CONSISTENCY_ENABLED:
            return []
        extra = max(1, settings.AGENTIC_SELF_CONSISTENCY_N) - 1
        if extra <= 0:
            return []
        futures = []
        try:
            base = list(self.sql_message) + [
                HumanMessage(self.chat_question.sql_user_question(
                    current_time=current_prompt_time(),
                    change_title=False)),
                HumanMessage(self.chat_question.alt_candidate_hint()),
            ]
            alt_llm = None
            alt_name = (settings.AGENTIC_SELF_CONSISTENCY_MODEL or '').strip()
            if alt_name and alt_name != self.config.model_name:
                try:
                    alt_llm = LLMFactory.create_llm(
                        self.config.model_copy(update={'model_name': alt_name})).llm
                except Exception:
                    # a broken alternate must not cost the vote itself
                    traceback.print_exc()
            for i in range(extra):
                futures.append(executor.submit(
                    self._invoke_llm_blocking, list(base),
                    alt_llm if (alt_llm is not None and i == 0) else None))
        except Exception:
            traceback.print_exc()
        return futures

    def _parse_candidate_sql(self, text: str) -> Optional[str]:
        """Extract SQL from a candidate answer without any logging/saving side
        effects (check_sql writes error logs and is tied to the main record)."""
        try:
            json_str = extract_nested_json(text or '', prefer_keys=('sql', 'success'))
            if not json_str:
                return None
            data = orjson.loads(json_str)
            if data.get('success') is False:
                return None
            sql = data.get('sql')
            if not isinstance(sql, str) or not sql.strip():
                return None
            return sql
        except Exception:
            return None

    def _apply_self_consistency(self, _session: Session, primary_sql: str, primary_execute_sql: str,
                                primary_result: dict, futures: list) -> Optional[dict]:
        """Execute the parallel candidates and return the plurality-agreed answer.

        Returns ``{'sql', 'execute_sql', 'result', 'votes', 'total_votes'}`` when
        consensus selects a DIFFERENT answer than the primary, else None (the
        primary already carries the plurality, so nothing needs to change).

        Every candidate goes through the same identifier check and LIMIT lifting
        as the primary; candidates that fail to parse, fail validation or fail to
        execute simply do not vote. Any internal error abandons voting and keeps
        the primary — consensus is an accuracy aid and must never lose an answer
        that already works."""
        if not futures:
            return None
        try:
            candidates: list[dict] = [{
                'sql': primary_sql,
                'execute_sql': primary_execute_sql,
                'result': primary_result,
                'ok': True,
                'fingerprint': result_fingerprint(primary_result.get('fields'),
                                                  primary_result.get('data')),
            }]

            timeout = max(1, settings.AGENTIC_SELF_CONSISTENCY_TIMEOUT)
            deadline = time.time() + timeout
            dropped = {'timeout': 0, 'parse': 0, 'identifier': 0, 'execute': 0}
            for future in futures:
                remaining = max(0.0, deadline - time.time())
                try:
                    text, _, _ = future.result(timeout=remaining)
                except Exception:
                    dropped['timeout'] += 1
                    continue
                candidate_sql = self._parse_candidate_sql(text)
                if not candidate_sql:
                    dropped['parse'] += 1
                    continue
                if self.validate_identifiers(candidate_sql):
                    dropped['identifier'] += 1
                    continue
                execute_sql = self._apply_nulls_last(self._maybe_raise_limit(candidate_sql))
                try:
                    candidate_result = self.execute_sql(sql=execute_sql)
                except Exception:
                    dropped['execute'] += 1
                    continue
                candidates.append({
                    'sql': candidate_sql,
                    'execute_sql': execute_sql,
                    'result': candidate_result,
                    'ok': True,
                    'fingerprint': result_fingerprint(candidate_result.get('fields'),
                                                      candidate_result.get('data')),
                })

            if len(candidates) < 2:
                SQLBotLogUtil.info(
                    f'self-consistency: no usable extra candidate '
                    f'({len(futures)} spawned, dropped={dropped})')
                return None

            winner = select_by_consensus(candidates)
            if not winner:
                return None
            SQLBotLogUtil.info(
                f'self-consistency: {winner.get("votes")}/{winner.get("total_votes")} agreement '
                f'across {len(candidates)} executed candidate(s), dropped={dropped}')
            if winner.get('fingerprint') == candidates[0].get('fingerprint'):
                return None
            return winner
        except Exception:
            traceback.print_exc()
            return None

    def _has_row_restrictions(self, _session: Session) -> bool:
        """True when row-level rules actually apply to this caller.

        Used to gate cross-datasource fanout, whose secondary legs bypass the
        permission rewrite. Fails CLOSED: any lookup problem reports "restricted"
        so an error can only ever disable fanout, never enable it for a user
        whose rows are filtered (AUDIT D-05 option 1).
        """
        try:
            if not isinstance(self.ds, CoreDatasource):
                return True
            if not is_normal_user(self.current_user):
                return False  # bypasses row rules entirely
            from apps.datasource.crud.table import get_readable_table_names
            _tables = get_readable_table_names(_session, self.ds.oid, self.ds.id)
            if not _tables:
                return False
            return bool(get_row_permission_filters(
                session=_session, current_user=self.current_user,
                ds=self.ds, tables=_tables))
        except Exception:
            traceback.print_exc()
            return True

    def decompose_question(self, _session: Session) -> dict:
        """Detect multi-datasource questions and route them. Returns
        ``{'mode': 'single'|'fanout'|'split', 'subs': [...]}``.
          - fanout: the same kind of data lives in several datasources -> run the
            same question against each and UNION the results.
          - split: the answer needs different facts from different datasources.
        Conservative gates: auto-selected ds only, no assistant context, and not a
        row-permission restricted user (secondary legs bypass the permission rewrite)."""
        single = {'mode': 'single', 'subs': []}
        if not settings.AGENTIC_DECOMPOSE_ENABLED or not self.ds_auto_selected:
            return single
        if not self.ds_candidates or not self._ranked_candidates:
            return single
        if self.current_assistant:
            return single
        # Secondary legs bypass the row-permission rewrite (_run_secondary_leg
        # never calls generate_filter), so fanout is unsafe for a user whose
        # rows are restricted. This used to be approximated as
        # `is_normal_user(...)` -- i.e. "is not user id 1" -- which disabled the
        # whole documented multi-file feature for every real account, not just
        # restricted ones (AUDIT D-05 option 1 / D-12).
        #
        # Gate on whether row filters ACTUALLY apply to this caller instead.
        # Deliberately conservative: any row rule anywhere in the datasource
        # blocks fanout, and a lookup failure blocks it too, so this can only
        # ever be more restrictive than the permission rewrite it stands in for.
        if self._has_row_restrictions(_session):
            return single
        try:
            valid_ids = {c.get('id') for c in self._ranked_candidates if isinstance(c.get('id'), int)}
            if len(valid_ids) < 2:
                return single
            primary_id = self.ds.id

            # --- deterministic, embedding-driven fanout candidates -----------
            # For a "list/find all X" question, every datasource whose similarity
            # is close to the primary's likely holds the same kind of entity.
            # We fan out to all of them; legs that don't actually contain X simply
            # return 0 rows and drop out of the union, so over-inclusion is safe.
            scores = {c.get('id'): float(c.get('cosine') or 0.0) for c in self._ranked_candidates}
            primary_score = scores.get(primary_id, 0.0)
            threshold = max(settings.AGENTIC_FANOUT_COSINE_FLOOR,
                            primary_score - settings.AGENTIC_FANOUT_COSINE_MARGIN)
            _max_secondary = max(1, settings.AGENTIC_FANOUT_MAX_SOURCES - 1)
            co_relevant = [c.get('id') for c in self._ranked_candidates
                           if c.get('id') != primary_id and c.get('id') in valid_ids
                           and float(c.get('cosine') or 0.0) >= threshold][:_max_secondary]
            is_retrieval = is_listing_question(
                self._original_question,
                _csv_terms(settings.AGENTIC_LISTING_KEYWORDS),
                _csv_terms(settings.AGENTIC_AGGREGATION_KEYWORDS))

            # --- LLM router (handles split + confirms fanout) ----------------
            msgs: List[Union[BaseMessage, dict[str, Any]]] = [
                SystemPromptMessage(self.chat_question.decompose_sys_question(primary_ds_id=primary_id)),
                HumanMessage(self.chat_question.decompose_user_question(
                    orjson.dumps(self._ranked_candidates).decode()))]
            text, _, _ = self._invoke_llm_blocking(msgs)
            verdict = parse_decomposition(text, valid_ids)
            subs = verdict.get('subs', [])
            llm_mode = verdict.get('mode', 'single')

            # split (different facts per datasource): trust the router verbatim
            if llm_mode == 'split' and len(subs) >= 2 and subs[0].get('ds_id') == primary_id:
                SQLBotLogUtil.info(f'agentic decomposition (llm split): {verdict}')
                return verdict

            # FANOUT recall: union the router's fanout picks with the
            # embedding-co-relevant datasources (for retrieval questions). Either
            # signal alone can miss a file; together they maximize coverage, and
            # empty legs self-prune from the union.
            fanout_ids: list[int] = []
            if llm_mode == 'fanout' and len(subs) >= 2 and subs[0].get('ds_id') == primary_id:
                fanout_ids = [s['ds_id'] for s in subs]
            if is_retrieval:
                for cid in co_relevant:
                    if cid not in fanout_ids:
                        fanout_ids.append(cid)
            fanout_ids = [i for i in fanout_ids if i != primary_id]
            fanout_ids = [primary_id] + fanout_ids
            fanout_ids = fanout_ids[:max(2, settings.AGENTIC_FANOUT_MAX_SOURCES)]  # cap total sources
            if len(fanout_ids) >= 2:
                SQLBotLogUtil.info(
                    f'agentic decomposition (fanout): primary={primary_id} score={primary_score:.3f} '
                    f'thr={threshold:.3f} llm={subs} co={co_relevant} -> {fanout_ids}')
                return {'mode': 'fanout',
                        'subs': [{'ds_id': i, 'question': self._original_question} for i in fanout_ids]}
            return single
        except Exception:
            traceback.print_exc()
            return single

    def _run_secondary_leg(self, _session: Session, sub: dict, raw: bool = False) -> Optional[dict]:
        """Run one cross-datasource sub-question internally (no streaming, no
        chart): switch datasource, generate SQL (one retry), execute, and return
        a summary. With raw=True returns the actual rows ('fields'/'data') for a
        deterministic UNION; otherwise a compact text preview for synthesis.
        Never raises."""
        try:
            ds_name = ''
            self.sql_message = []
            self.chat_question.question = sub['question']
            self._skip_apex_refinement = True
            self._skip_leg_history = True
            try:
                self._apply_datasource(_session, sub['ds_id'])
            finally:
                self._skip_apex_refinement = False
                self._skip_leg_history = False
            ds_name = getattr(self.ds, 'name', '') or ''
            feedback = None
            attempts = (2 + max(0, settings.AGENTIC_LEG_EMPTY_RETRIES)
                        if self._multi_mode == 'fanout' else 2)
            for _i in range(attempts):
                if feedback is None:
                    self.sql_message.append(HumanMessage(
                        self.chat_question.sql_user_question(
                            current_time=current_prompt_time(), change_title=False)))
                else:
                    self.sql_message.append(HumanMessage(
                        self.chat_question.sql_retry_user_question(
                            feedback=feedback, current_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))))
                text, _, _ = self._invoke_llm_blocking(self.sql_message)
                self.sql_message.append(AIMessage(text))
                json_str = extract_nested_json(text, prefer_keys=('sql', 'success'))
                data = None
                if json_str:
                    try:
                        data = orjson.loads(json_str)
                    except Exception:
                        data = None
                # tolerant: accept SQL even when the model omits the 'success'
                # flag; only an explicit success=false is a refusal
                leg_sql = data.get('sql') if isinstance(data, dict) else None
                if not data or data.get('success') is False or not isinstance(leg_sql, str) \
                        or not leg_sql.strip():
                    feedback = format_retry_feedback('parse', 'answer was not a valid SQL JSON object', None)
                    continue
                # legs of a listing question: lift an over-small model LIMIT
                leg_sql = self._apply_nulls_last(self._maybe_raise_limit(leg_sql))
                try:
                    result = self.execute_sql(sql=leg_sql)
                except Exception as e:
                    feedback = format_retry_feedback('execute', str(e)[:settings.LLM_ERROR_TEXT_MAX_CHARS], leg_sql)
                    continue
                # FANOUT completeness: an empty result is often a value-format
                # mismatch across files (the same category value stored with a
                # different case, spelling, plural, or combined with others).
                # Retry with a fuzzy-matching hint before accepting empty, so a
                # file that really does contain the entity still contributes.
                if self._multi_mode == 'fanout' and not result.get('data') and _i < attempts - 1:
                    feedback = format_retry_feedback(
                        'empty-result',
                        "The query returned 0 rows. The filter value may be stored differently in "
                        "this file than expected: a singular/plural variant, different letter case, "
                        "surrounding whitespace, or combined with other values inside one cell. "
                        "Retry using case-insensitive PARTIAL matching (e.g. column ILIKE '%value%' "
                        "or LOWER(column) LIKE '%value%') instead of exact equality, and consider "
                        "matching against multi-value cells. Keep the user's original intent.", leg_sql)
                    continue
                if raw:
                    return {'datasource': ds_name, 'question': sub['question'], 'sql': leg_sql,
                            'fields': result.get('fields') or [], 'data': result.get('data') or []}
                # fanout legs feed a UNION, so capture many more rows than a
                # split leg (which only needs a compact factual summary)
                if self._multi_mode == 'fanout':
                    preview = build_result_preview(result.get('fields'), result.get('data'),
                                                   max_rows=settings.AGENTIC_LEG_MAX_ROWS, max_chars=12000)
                else:
                    preview = build_result_preview(result.get('fields'), result.get('data'))
                return {'datasource': ds_name, 'question': sub['question'], 'sql': leg_sql,
                        'result_preview': preview}
            if raw:
                return {'datasource': ds_name, 'question': sub['question'], 'sql': None,
                        'fields': [], 'data': []}
            return {'datasource': ds_name, 'question': sub['question'], 'sql': None,
                    'result_preview': 'FAILED: could not produce a working SQL for this sub-question'}
        except Exception:
            traceback.print_exc()
            return None

    def synthesize_answer(self, _session: Session, legs: list[dict]):
        """Stream the merged cross-datasource answer (union for fanout)."""
        msgs: List[Union[BaseMessage, dict[str, Any]]] = [
            SystemPromptMessage(self.chat_question.synthesize_sys_question()),
            HumanMessage(self.chat_question.synthesize_user_question(
                original_question=self._original_question,
                legs_json=json.dumps(legs, ensure_ascii=False),
                mode=self._multi_mode))]
        token_usage: dict = {}
        res = process_stream(self.llm.stream(msgs), token_usage)
        for chunk in res:
            yield chunk

    # ------------------------------------------------------------------
    # APEX-SQL: schema-agnostic logical planning with N=2 consensus
    # ------------------------------------------------------------------
    def _invoke_llm_blocking(self, msgs: List[Union[BaseMessage, dict[str, Any]]],
                             llm: Optional[BaseChatModel] = None) -> tuple[str, str, dict]:
        """Run a non-streamed LLM call and return (text, thinking, token_usage).

        `llm` overrides the primary model for this one call (cross-model
        self-consistency candidates); None uses the session's model."""
        text = ''
        thinking = ''
        token_usage: dict = {}
        try:
            res = process_stream((llm or self.llm).stream(msgs), token_usage)
            for chunk in res:
                if chunk.get('content'):
                    text += chunk.get('content')
                if chunk.get('reasoning_content'):
                    thinking += chunk.get('reasoning_content')
        except Exception as e:
            SQLBotLogUtil.info(f"LLM call failed: {e}")
        return text, thinking, token_usage

    def generate_logical_plan(self, _session: Session):
        """Schema-agnostic logical planning with N=2 consensus aggregation.

        Per APEX-SQL §3.2.1: sample N candidates independently, then aggregate them
        into one master plan. This reduces noise and improves downstream pruning by
        ~5% SRR according to their ablation. All steps are graceful on failure — if
        anything errors out, we proceed without a plan rather than break the chat.
        """
        if not self.chat_question.question or not self.chat_question.question.strip():
            return

        try:
            self.current_logs[OperationEnum.LOGICAL_PLANNING] = start_log(
                session=_session,
                ai_modal_id=self.chat_question.ai_modal_id,
                ai_modal_name=self.chat_question.ai_modal_name,
                operate=OperationEnum.LOGICAL_PLANNING,
                record_id=self.record.id,
                full_message=[])

            # Stage 1: sample N=2 candidate plans in parallel.
            def _sample_one() -> str:
                msgs: List[Union[BaseMessage, dict[str, Any]]] = [
                    SystemPromptMessage(content=self.chat_question.planning_sys_question()),
                    HumanMessage(content=self.chat_question.planning_user_question()),
                ]
                text, _, _ = self._invoke_llm_blocking(msgs)
                return text.strip()

            with ThreadPoolExecutor(max_workers=settings.APEX_MAX_WORKERS) as ex:
                futures = [ex.submit(_sample_one) for _ in range(2)]
                candidates = [f.result() for f in futures]
            candidates = [c for c in candidates if c]

            if not candidates:
                self.chat_question.logical_plan = ''
                self.current_logs[OperationEnum.LOGICAL_PLANNING] = end_log(
                    session=_session,
                    log=self.current_logs[OperationEnum.LOGICAL_PLANNING],
                    full_message=[{'type': 'system', 'content': '(no candidates produced)'}])
                return

            # Stage 2: aggregate (if 1 candidate, use it directly; if 2+, synthesise).
            if len(candidates) == 1:
                master_plan = candidates[0]
                agg_msgs: List[Union[BaseMessage, dict[str, Any]]] = []
                agg_thinking = ''
                agg_usage: dict = {}
            else:
                draft_text = '\n\n'.join(f"--- Draft {i+1} ---\n{c}" for i, c in enumerate(candidates))
                agg_msgs = [
                    SystemPromptMessage(content=self.chat_question.planning_aggregate_sys_question()),
                    HumanMessage(content=self.chat_question.planning_aggregate_user_question(draft_text)),
                ]
                master_plan, agg_thinking, agg_usage = self._invoke_llm_blocking(agg_msgs)
                master_plan = master_plan.strip() or candidates[0]

            self.chat_question.logical_plan = master_plan

            full_msg = [
                {'type': 'system', 'content': 'Logical Planning (N=2 consensus)'},
                *[{'type': 'ai', 'content': f'Draft {i+1}:\n{c}'} for i, c in enumerate(candidates)],
            ]
            if agg_msgs:
                full_msg.extend([{'type': m.type, 'content': m.content} for m in agg_msgs])
                full_msg.append({'type': 'ai', 'content': master_plan})

            self.current_logs[OperationEnum.LOGICAL_PLANNING] = end_log(
                session=_session,
                log=self.current_logs[OperationEnum.LOGICAL_PLANNING],
                full_message=full_msg,
                reasoning_content=agg_thinking,
                token_usage=agg_usage)
        except Exception as e:
            SQLBotLogUtil.info(f"Logical planning failed, proceeding without plan: {e}")
            self.chat_question.logical_plan = ''

    # ------------------------------------------------------------------
    # APEX-SQL: Dual-Pathway Pruning (negative + positive parallel passes, union)
    # ------------------------------------------------------------------
    def prune_schema_dual_pathway(self, _session: Session):
        """Per APEX-SQL §3.2.2: simultaneously run a negative pass (mark obviously
        irrelevant tables/columns) and a positive pass (mark relevant/necessary
        tables/columns), then union the results so an ambiguous element is preserved
        unless it is BOTH confidently rejected AND not explicitly selected.

        Skips when:
          - logical_plan is empty (we have no reasoning anchor)
          - schema is small (<= APEX_MIN_COLUMNS_TO_PRUNE columns) — pruning offers no benefit
          - the LLM call fails — we keep the original schema rather than risk a
            bad prune dropping critical columns.
        """
        from apps.chat.task.apex_helpers import (
            parse_schema, rebuild_schema, apply_pruning, batch_tables_by_token_budget,
            merge_keep_sets, safe_json_loads,
        )

        if not self.chat_question.db_schema:
            return
        if not self.chat_question.logical_plan:
            SQLBotLogUtil.info("Dual-pathway pruning skipped: no logical plan available.")
            return

        try:
            parsed = parse_schema(self.chat_question.db_schema)
            total_cols = sum(len(t.get('columns', [])) for t in parsed.get('tables', []))
            if total_cols <= settings.APEX_MIN_COLUMNS_TO_PRUNE:
                SQLBotLogUtil.info(f"Dual-pathway pruning skipped: schema too small ({total_cols} cols).")
                return

            self.current_logs[OperationEnum.PRUNE_SCHEMA] = start_log(
                session=_session,
                ai_modal_id=self.chat_question.ai_modal_id,
                ai_modal_name=self.chat_question.ai_modal_name,
                operate=OperationEnum.PRUNE_SCHEMA,
                record_id=self.record.id,
                full_message=[])

            batches = batch_tables_by_token_budget(parsed, approx_tokens_per_batch=10_000)

            del_tables_per_batch: List[List[str]] = []
            del_cols_per_batch: List[Dict[str, List[str]]] = []
            keep_tables_per_batch: List[List[str]] = []
            keep_cols_per_batch: List[Dict[str, List[str]]] = []
            log_messages: List[Dict[str, Any]] = []

            for batch in batches:
                batch_str = rebuild_schema(batch)

                def _pass_negative() -> Optional[Dict[str, Any]]:
                    msgs: List[Union[BaseMessage, dict[str, Any]]] = [
                        SystemPromptMessage(content=self.chat_question.prune_delete_sys_question()),
                        HumanMessage(content=self.chat_question.prune_delete_user_question(batch_str)),
                    ]
                    text, _, _ = self._invoke_llm_blocking(msgs)
                    return safe_json_loads(text)

                def _pass_positive() -> Optional[Dict[str, Any]]:
                    msgs: List[Union[BaseMessage, dict[str, Any]]] = [
                        SystemPromptMessage(content=self.chat_question.prune_select_sys_question()),
                        HumanMessage(content=self.chat_question.prune_select_user_question(batch_str)),
                    ]
                    text, _, _ = self._invoke_llm_blocking(msgs)
                    return safe_json_loads(text)

                with ThreadPoolExecutor(max_workers=settings.APEX_MAX_WORKERS) as ex:
                    f_neg = ex.submit(_pass_negative)
                    f_pos = ex.submit(_pass_positive)
                    neg_obj = f_neg.result() or {}
                    pos_obj = f_pos.result() or {}

                neg_tables = neg_obj.get('obviously_irrelevant_tables') or []
                neg_cols_raw = neg_obj.get('obviously_irrelevant_columns') or []
                pos_tables = pos_obj.get('relevant_tables') or []
                pos_cols_raw = pos_obj.get('relevant_columns') or []

                neg_cols: Dict[str, List[str]] = {}
                for entry in neg_cols_raw:
                    if isinstance(entry, dict) and entry.get('table'):
                        neg_cols.setdefault(entry['table'], []).extend(entry.get('columns') or [])
                pos_cols: Dict[str, List[str]] = {}
                for entry in pos_cols_raw:
                    if isinstance(entry, dict) and entry.get('table'):
                        pos_cols.setdefault(entry['table'], []).extend(entry.get('columns') or [])

                del_tables_per_batch.append(neg_tables)
                del_cols_per_batch.append(neg_cols)
                keep_tables_per_batch.append(pos_tables)
                keep_cols_per_batch.append(pos_cols)
                log_messages.append({'type': 'ai', 'content': f"Negative: {neg_obj}\nPositive: {pos_obj}"})

            del_t, del_c, keep_t, keep_c = merge_keep_sets(
                parsed, del_tables_per_batch, del_cols_per_batch,
                keep_tables_per_batch, keep_cols_per_batch)

            pruned_parsed = apply_pruning(parsed, del_t, del_c, keep_t, keep_c)
            # Safety net: if pruning would remove EVERYTHING, abort and keep original.
            if not pruned_parsed.get('tables'):
                SQLBotLogUtil.info("Dual-pathway pruning would remove all tables; aborting prune.")
                self.current_logs[OperationEnum.PRUNE_SCHEMA] = end_log(
                    session=_session,
                    log=self.current_logs[OperationEnum.PRUNE_SCHEMA],
                    full_message=log_messages + [{'type': 'system', 'content': 'aborted: would remove all tables'}])
                return

            new_schema = rebuild_schema(pruned_parsed)
            kept_cols_count = sum(len(t.get('columns', [])) for t in pruned_parsed.get('tables', []))
            self.chat_question.db_schema = new_schema
            log_messages.append({
                'type': 'system',
                'content': f"Pruned schema: {len(parsed['tables'])} tables / {total_cols} cols -> "
                           f"{len(pruned_parsed['tables'])} tables / {kept_cols_count} cols",
            })

            self.current_logs[OperationEnum.PRUNE_SCHEMA] = end_log(
                session=_session,
                log=self.current_logs[OperationEnum.PRUNE_SCHEMA],
                full_message=log_messages)
        except Exception as e:
            SQLBotLogUtil.info(f"Dual-pathway pruning failed, keeping original schema: {e}")

    # ------------------------------------------------------------------
    # APEX-SQL: Parallel Data Profiling with role hypothesis + per-table agents
    # ------------------------------------------------------------------
    def profile_data_parallel(self, _session: Session):
        """Per APEX-SQL §3.2.3: for each surviving table, in parallel:
          1) Hypothesise the table's role (Target / Bridge / Filter / Calculation).
          2) Generate 3-8 SELECT probes grounded in that role.
          3) Execute them read-only against the live datasource.
          4) Compress results into a statistical summary.
        Findings are concatenated and exposed via chat_question.data_observations,
        which is then injected into the SQL user prompt as <data-observations>.

        Skipped if there is no live datasource (e.g. assistant out-ds without
        executable query support) or if the schema is empty.
        """
        from apps.chat.task.apex_helpers import (
            parse_schema, rebuild_schema, compress_probe_result, safe_json_loads,
            quoted_table_ref, quoted_columns_list, is_probe_failure_summary,
        )

        if not self.ds or not self.chat_question.db_schema:
            return
        if not self.chat_question.logical_plan:
            return

        try:
            parsed = parse_schema(self.chat_question.db_schema)
            tables = parsed.get('tables', [])
            if not tables:
                return

            # Cap on how many tables we'll probe to bound cost/latency.
            tables_to_probe = tables[:settings.APEX_MAX_PROBE_TABLES]
            ds_type = getattr(self.ds, 'type', None) or ''
            db_id = parsed.get('db_id', '') or ''

            self.current_logs[OperationEnum.DATA_PROFILING] = start_log(
                session=_session,
                ai_modal_id=self.chat_question.ai_modal_id,
                ai_modal_name=self.chat_question.ai_modal_name,
                operate=OperationEnum.DATA_PROFILING,
                record_id=self.record.id,
                full_message=[])

            def _profile_one(table_entry: Dict[str, Any]) -> Dict[str, Any]:
                table_name = table_entry.get('table_name', '')
                single_schema = rebuild_schema({
                    'db_id': parsed.get('db_id', ''),
                    'header_prefix': parsed.get('header_prefix', ''),
                    'tables': [table_entry],
                })

                # Build pre-quoted identifiers so the probe LLM can't get quoting wrong.
                q_table = quoted_table_ref(ds_type, db_id, table_name)
                q_cols = quoted_columns_list(ds_type, table_entry.get('columns', []))
                q_cols_block = '\n'.join(f"  - {c}" for c in q_cols) or '  (no columns)'

                # Step 1: hypothesise role.
                role_msgs: List[Union[BaseMessage, dict[str, Any]]] = [
                    SystemPromptMessage(content=self.chat_question.profile_role_sys_question()),
                    HumanMessage(content=self.chat_question.profile_role_user_question(single_schema)),
                ]
                role_text, _, _ = self._invoke_llm_blocking(role_msgs)
                role_obj = safe_json_loads(role_text) or {}
                roles = role_obj.get('roles') or ['Unknown']
                roles_str = ', '.join(roles) if isinstance(roles, list) else str(roles)

                # Step 2: generate probes with explicit quoted-identifier hints.
                probe_msgs: List[Union[BaseMessage, dict[str, Any]]] = [
                    SystemPromptMessage(content=self.chat_question.profile_probe_sys_question(roles_str)),
                    HumanMessage(content=self.chat_question.profile_probe_user_question(
                        single_schema, q_table, q_cols_block)),
                ]
                probe_text, _, _ = self._invoke_llm_blocking(probe_msgs)
                probe_obj = safe_json_loads(probe_text) or {}
                probes = probe_obj.get('probes') or []

                # Step 3: execute probes. CRITICAL: never expose raw DB error text to the
                # downstream SQL-gen LLM. Specific errors like "relation does not exist"
                # have been observed to fool the next model into asserting the table is
                # missing. We track which probes succeeded and only include successes
                # in the observations sent downstream.
                success_findings: List[str] = []
                fail_count = 0
                for p in probes[:settings.APEX_MAX_PROBES]:
                    if not isinstance(p, dict):
                        continue
                    sql = (p.get('sql') or '').strip()
                    motivation = (p.get('motivation') or '').strip()
                    if not sql:
                        continue
                    lower = sql.lower().lstrip()
                    if not (lower.startswith('select') or lower.startswith('with')):
                        continue
                    try:
                        result = exec_sql(ds=self.ds, sql=sql, origin_column=False)
                        summary = compress_probe_result(result)
                        if is_probe_failure_summary(summary):
                            fail_count += 1
                            continue
                        success_findings.append(
                            f"- Motivation: {motivation}\n  SQL: {sql}\n  Result: {summary}"
                        )
                    except Exception:
                        # Intentionally swallow the exception text — log only.
                        SQLBotLogUtil.info(f"Probe failed for table {table_name} (silenced for prompt).")
                        fail_count += 1

                # If 100% of probes failed (or no probes), DROP this table from observations
                # entirely. Mentioning the table at all here would just confuse downstream.
                if not success_findings:
                    return {
                        'table_name': table_name,
                        'roles': roles_str,
                        'observations': '',
                        'success_count': 0,
                        'fail_count': fail_count,
                    }
                return {
                    'table_name': table_name,
                    'roles': roles_str,
                    'observations': '\n'.join(success_findings),
                    'success_count': len(success_findings),
                    'fail_count': fail_count,
                }

            with ThreadPoolExecutor(max_workers=min(4, len(tables_to_probe) or 1)) as ex:
                results = list(ex.map(_profile_one, tables_to_probe))

            # Compose observations from tables with at least one successful probe.
            obs_parts: List[str] = []
            for r in results:
                if r.get('success_count', 0) > 0 and r.get('observations'):
                    obs_parts.append(
                        f"Table `{r['table_name']}` (role: {r['roles']}):\n{r['observations']}"
                    )

            self.chat_question.data_observations = '\n\n'.join(obs_parts)

            summary_line = (
                f"Profiled {len(results)} tables; "
                f"{sum(1 for r in results if r.get('success_count', 0) > 0)} produced usable findings, "
                f"{sum(1 for r in results if r.get('success_count', 0) == 0)} dropped (no successful probes)."
            )
            log_msgs = [
                {'type': 'system', 'content': summary_line},
                {'type': 'ai', 'content': self.chat_question.data_observations[:8000] or '(none)'},
            ]
            self.current_logs[OperationEnum.DATA_PROFILING] = end_log(
                session=_session,
                log=self.current_logs[OperationEnum.DATA_PROFILING],
                full_message=log_msgs)
        except Exception as e:
            SQLBotLogUtil.info(f"Data profiling failed, proceeding without observations: {e}")
            self.chat_question.data_observations = ''

    def generate_sql(self, _session: Session, feedback: Optional[str] = None):
        # append current question (first attempt) or the retry-feedback prompt
        # (agentic retry attempts; the failed AI answer is already in history)
        if feedback is None:
            self.sql_message.append(HumanMessage(
                self.chat_question.sql_user_question(current_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                                     change_title=self.change_title)))
        else:
            self.sql_message.append(HumanMessage(
                self.chat_question.sql_retry_user_question(feedback=feedback,
                                                           current_time=datetime.now().strftime(
                                                               '%Y-%m-%d %H:%M:%S'))))

        self.current_logs[OperationEnum.GENERATE_SQL] = start_log(session=_session,
                                                                  ai_modal_id=self.chat_question.ai_modal_id,
                                                                  ai_modal_name=self.chat_question.ai_modal_name,
                                                                  operate=OperationEnum.GENERATE_SQL,
                                                                  record_id=self.record.id,
                                                                  full_message=[
                                                                      {'type': msg.type,
                                                                       'sqlbot_system': getattr(msg, 'sqlbot_system',
                                                                                                False) is True,
                                                                       'content': msg.content} for msg
                                                                      in self.sql_message])
        # Prompt SIZE, not content (the content is already persisted in chat_log.messages).
        # 22% of calls were measured at >=32k tokens against a 32,768 context, which
        # truncates silently and yields an empty answer rather than an error, so the
        # size is the number worth having in the log when an answer comes back empty.
        # chars/token measured at ~2.9 on this workload (46,933 chars -> 16,222 real
        # input_tokens), NOT the usual ~4: schema blocks are dense in identifiers,
        # quotes and punctuation. Using 4 understated the true size by ~40%, which
        # is exactly the wrong direction for a context-ceiling warning.
        _prompt_chars = sum(len(str(getattr(m, 'content', '') or '')) for m in self.sql_message)
        SQLBotLogUtil.info(
            f'generate_sql prompt: {_prompt_chars} chars (~{int(_prompt_chars / 2.9)} tokens est) '
            f'across {len(self.sql_message)} message(s)')

        full_thinking_text = ''
        full_sql_text = ''
        token_usage = {}
        res = process_stream(self.llm.stream(self.sql_message), token_usage)
        for chunk in res:
            if chunk.get('content'):
                full_sql_text += chunk.get('content')
            if chunk.get('reasoning_content'):
                full_thinking_text += chunk.get('reasoning_content')
            yield chunk

        self.sql_message.append(AIMessage(full_sql_text))

        self.current_logs[OperationEnum.GENERATE_SQL] = end_log(session=_session,
                                                                log=self.current_logs[OperationEnum.GENERATE_SQL],
                                                                full_message=[{'type': msg.type,
                                                                               'sqlbot_system': getattr(msg,
                                                                                                        'sqlbot_system',
                                                                                                        False) is True,
                                                                               'content': msg.content}
                                                                              for msg in self.sql_message],
                                                                reasoning_content=full_thinking_text,
                                                                token_usage=token_usage)
        self.record = save_sql_answer(session=_session, record_id=self.record.id,
                                      answer=orjson.dumps({'content': full_sql_text}).decode())

    def generate_with_sub_sql(self, session: Session, sql, sub_mappings: list):
        sub_query = json.dumps(sub_mappings, ensure_ascii=False)
        self.chat_question.sql = sql
        self.chat_question.sub_query = sub_query
        dynamic_sql_msg: List[Union[BaseMessage, dict[str, Any]]] = []
        dynamic_sql_msg.append(SystemPromptMessage(content=self.chat_question.dynamic_sys_question()))
        dynamic_sql_msg.append(HumanMessage(content=self.chat_question.dynamic_user_question()))

        self.current_logs[OperationEnum.GENERATE_DYNAMIC_SQL] = start_log(session=session,
                                                                          ai_modal_id=self.chat_question.ai_modal_id,
                                                                          ai_modal_name=self.chat_question.ai_modal_name,
                                                                          operate=OperationEnum.GENERATE_DYNAMIC_SQL,
                                                                          record_id=self.record.id,
                                                                          full_message=[{'type': msg.type,
                                                                                         'sqlbot_system': getattr(msg,
                                                                                                                  'sqlbot_system',
                                                                                                                  False) is True,
                                                                                         'content': msg.content}
                                                                                        for
                                                                                        msg in dynamic_sql_msg])

        full_thinking_text = ''
        full_dynamic_text = ''
        token_usage = {}
        res = process_stream(self.llm.stream(dynamic_sql_msg), token_usage)
        for chunk in res:
            if chunk.get('content'):
                full_dynamic_text += chunk.get('content')
            if chunk.get('reasoning_content'):
                full_thinking_text += chunk.get('reasoning_content')

        dynamic_sql_msg.append(AIMessage(full_dynamic_text))

        self.current_logs[OperationEnum.GENERATE_DYNAMIC_SQL] = end_log(session=session,
                                                                        log=self.current_logs[
                                                                            OperationEnum.GENERATE_DYNAMIC_SQL],
                                                                        full_message=[
                                                                            {'type': msg.type,
                                                                             'sqlbot_system': getattr(msg,
                                                                                                      'sqlbot_system',
                                                                                                      False) is True,
                                                                             'content': msg.content}
                                                                            for msg in dynamic_sql_msg],
                                                                        reasoning_content=full_thinking_text,
                                                                        token_usage=token_usage)

        SQLBotLogUtil.info(full_dynamic_text)
        return full_dynamic_text

    def generate_assistant_dynamic_sql(self, _session: Session, sql, tables: List):
        ds: AssistantOutDsSchema = self.ds
        sub_query = []
        result_dict = {}
        for table in ds.tables:
            if table.name in tables and table.sql:
                # sub_query.append({"table": table.name, "query": table.sql})
                result_dict[table.name] = table.sql
                sub_query.append({"table": table.name, "query": f'{dynamic_subsql_prefix}{table.name}'})
        if not sub_query:
            return None
        temp_sql_text = self.generate_with_sub_sql(session=_session, sql=sql, sub_mappings=sub_query)
        result_dict['sqlbot_temp_sql_text'] = temp_sql_text
        return result_dict

    def build_table_filter(self, session: Session, sql: str, filters: list):
        filter = json.dumps(filters, ensure_ascii=False)
        self.chat_question.sql = sql
        self.chat_question.filter = filter
        permission_sql_msg: List[Union[BaseMessage, dict[str, Any]]] = []
        permission_sql_msg.append(SystemPromptMessage(content=self.chat_question.filter_sys_question()))
        permission_sql_msg.append(HumanMessage(content=self.chat_question.filter_user_question()))

        self.current_logs[OperationEnum.GENERATE_SQL_WITH_PERMISSIONS] = start_log(session=session,
                                                                                   ai_modal_id=self.chat_question.ai_modal_id,
                                                                                   ai_modal_name=self.chat_question.ai_modal_name,
                                                                                   operate=OperationEnum.GENERATE_SQL_WITH_PERMISSIONS,
                                                                                   record_id=self.record.id,
                                                                                   full_message=[
                                                                                       {'type': msg.type,
                                                                                        'sqlbot_system': getattr(msg,
                                                                                                                 'sqlbot_system',
                                                                                                                 False) is True,
                                                                                        'content': msg.content} for
                                                                                       msg
                                                                                       in permission_sql_msg])
        full_thinking_text = ''
        full_filter_text = ''
        token_usage = {}
        res = process_stream(self.llm.stream(permission_sql_msg), token_usage)
        for chunk in res:
            if chunk.get('content'):
                full_filter_text += chunk.get('content')
            if chunk.get('reasoning_content'):
                full_thinking_text += chunk.get('reasoning_content')

        permission_sql_msg.append(AIMessage(full_filter_text))

        self.current_logs[OperationEnum.GENERATE_SQL_WITH_PERMISSIONS] = end_log(session=session,
                                                                                 log=self.current_logs[
                                                                                     OperationEnum.GENERATE_SQL_WITH_PERMISSIONS],
                                                                                 full_message=[
                                                                                     {'type': msg.type,
                                                                                      'sqlbot_system': getattr(msg,
                                                                                                               'sqlbot_system',
                                                                                                               False) is True,
                                                                                      'content': msg.content}
                                                                                     for msg in permission_sql_msg],
                                                                                 reasoning_content=full_thinking_text,
                                                                                 token_usage=token_usage)

        SQLBotLogUtil.info(full_filter_text)
        return full_filter_text

    def generate_filter(self, _session: Session, sql: str, tables: List):
        filters = get_row_permission_filters(session=_session, current_user=self.current_user, ds=self.ds,
                                             tables=tables)
        if not filters:
            return None
        return self.build_table_filter(session=_session, sql=sql, filters=filters)

    def generate_assistant_filter(self, _session: Session, sql, tables: List):
        ds: AssistantOutDsSchema = self.ds
        filters = []
        for table in ds.tables:
            if table.name in tables and table.rule:
                filters.append({"table": table.name, "filter": table.rule})
        if not filters:
            return None
        return self.build_table_filter(session=_session, sql=sql, filters=filters)

    def generate_chart(self, _session: Session, chart_type: Optional[str] = '', schema: Optional[str] = ''):
        # append current question
        self.chart_message.append(HumanMessage(self.chat_question.chart_user_question(chart_type, schema)))

        self.current_logs[OperationEnum.GENERATE_CHART] = start_log(session=_session,
                                                                    ai_modal_id=self.chat_question.ai_modal_id,
                                                                    ai_modal_name=self.chat_question.ai_modal_name,
                                                                    operate=OperationEnum.GENERATE_CHART,
                                                                    record_id=self.record.id,
                                                                    full_message=[
                                                                        {'type': msg.type,
                                                                         'sqlbot_system': getattr(msg, 'sqlbot_system',
                                                                                                  False) is True,
                                                                         'content': msg.content} for
                                                                        msg
                                                                        in self.chart_message])
        full_thinking_text = ''
        full_chart_text = ''
        token_usage = {}
        res = process_stream(self.llm.stream(self.chart_message), token_usage)
        for chunk in res:
            if chunk.get('content'):
                full_chart_text += chunk.get('content')
            if chunk.get('reasoning_content'):
                full_thinking_text += chunk.get('reasoning_content')
            yield chunk

        self.chart_message.append(AIMessage(full_chart_text))

        self.record = save_chart_answer(session=_session, record_id=self.record.id,
                                        answer=orjson.dumps({'content': full_chart_text}).decode())
        self.current_logs[OperationEnum.GENERATE_CHART] = end_log(session=_session,
                                                                  log=self.current_logs[OperationEnum.GENERATE_CHART],
                                                                  full_message=[
                                                                      {'type': msg.type,
                                                                       'sqlbot_system': getattr(msg, 'sqlbot_system',
                                                                                                False) is True,
                                                                       'content': msg.content}
                                                                      for msg in self.chart_message],
                                                                  reasoning_content=full_thinking_text,
                                                                  token_usage=token_usage)

    def check_sql(self, session: Session, res: str, operate: OperationEnum) -> tuple[str, Optional[list]]:
        # prefer the LAST object carrying the answer keys: a model that prefaces
        # its reply with an example object would otherwise have the example
        # parsed as its answer (AUDIT D-09).
        json_str = extract_nested_json(res, prefer_keys=('sql', 'success'))

        log = self.current_logs[operate]

        if json_str is None:
            trigger_log_error(session, log)
            raise SingleMessageError(orjson.dumps({'message': 'SQL answer is not a valid json object',
                                                   'traceback': "SQL answer is not a valid json object:\n" + res}).decode())
        sql: str
        data: dict
        try:
            data = orjson.loads(json_str)

            # Only an EXPLICIT success=false is a refusal. Many models omit the
            # 'success' flag while still returning valid SQL, so we extract the
            # SQL whenever it is present rather than hard-requiring the flag.
            if data.get('success') is False:
                message = data.get('message') or 'The model declined to answer this question'
                # Tag refusals so the agentic loop can offer ONE best-effort retry
                # before surfacing them. Measured on BIRD-150: qwen2.5-coder:32b
                # refused 13 questions (8.7%) vs gpt-oss:20b's ~1, almost always by
                # arguing with the question's hint ("the hint says MAX(dob) but
                # youngest means MIN(dob)") rather than by lacking the schema. Those
                # are recoverable; a second refusal still reaches the user.
                raise SingleMessageError(f'{_REFUSAL_TAG}{message}')
            sql = data.get('sql')
            if not isinstance(sql, str):
                raise KeyError('sql')
        except SingleMessageError as e:
            trigger_log_error(session, log)
            raise e
        except Exception:
            trigger_log_error(session, log)
            raise SingleMessageError(orjson.dumps({'message': 'Cannot parse sql from answer',
                                                   'traceback': "Cannot parse sql from answer:\n" + res}).decode())

        if sql.strip() == '':
            trigger_log_error(session, log)
            raise SingleMessageError("SQL query is empty")
        return sql, data.get('tables')

    @staticmethod
    def get_chart_type_from_sql_answer(res: str) -> Optional[str]:
        json_str = extract_nested_json(res, prefer_keys=('sql', 'success'))
        if json_str is None:
            return None

        data: dict
        try:
            data = orjson.loads(json_str)

            if data.get('success') is False:
                return None
            # accept both kebab-case (spec) and snake_case (some models)
            return data.get('chart-type') or data.get('chart_type')
        except Exception:
            return None

    @staticmethod
    def get_brief_from_sql_answer(res: str) -> Optional[str]:
        json_str = extract_nested_json(res, prefer_keys=('sql', 'success'))
        if json_str is None:
            return None

        data: dict
        try:
            data = orjson.loads(json_str)

            if data.get('success') is False:
                return None
            # accept the spec key and common model variants
            return data.get('brief') or data.get('dialogue_title') or data.get('title')
        except Exception:
            return None

    def check_save_sql(self, session: Session, res: str, operate: OperationEnum) -> str:
        sql, *_ = self.check_sql(session=session, res=res, operate=operate)
        save_sql(session=session, sql=sql, record_id=self.record.id)

        self.chat_question.sql = sql

        return sql

    def check_save_chart(self, session: Session, res: str) -> Dict[str, Any]:

        json_str = extract_nested_json(res, prefer_keys=('type',))
        if json_str is None:
            raise SingleMessageError(orjson.dumps({'message': 'Cannot parse chart config from answer',
                                                   'traceback': "Cannot parse chart config from answer:\n" + res}).decode())
        data: dict

        chart: Dict[str, Any] = {}
        message = ''
        error = False

        try:
            data = orjson.loads(json_str)
            if data['type'] and data['type'] != 'error':
                # todo type check
                chart = data
                if chart.get('columns'):
                    for v in chart.get('columns'):
                        v['value'] = v.get('value').lower()
                if chart.get('axis'):
                    if chart.get('axis').get('x'):
                        chart.get('axis').get('x')['value'] = chart.get('axis').get('x').get('value').lower()
                    y_axis = chart.get('axis').get('y')
                    if y_axis:
                        if isinstance(y_axis, list):
                            # 数组格式: y: [{name, value}, ...]
                            for item in y_axis:
                                if item.get('value'):
                                    item['value'] = item['value'].lower()
                        elif isinstance(y_axis, dict) and y_axis.get('value'):
                            # 旧格式: y: {name, value}
                            y_axis['value'] = y_axis['value'].lower()
                    if chart.get('axis').get('series'):
                        chart.get('axis').get('series')['value'] = chart.get('axis').get('series').get('value').lower()
                if chart.get('axis') and chart['axis'].get('multi-quota'):
                    multi_quota = chart['axis']['multi-quota']
                    if multi_quota.get('value'):
                        if isinstance(multi_quota['value'], list):
                            # 将数组中的每个值转换为小写
                            multi_quota['value'] = [v.lower() if v else v for v in multi_quota['value']]
                        elif isinstance(multi_quota['value'], str):
                            # 如果是字符串，也转换为小写
                            multi_quota['value'] = multi_quota['value'].lower()
            elif data['type'] == 'error':
                message = data['reason']
                error = True
            else:
                raise Exception('Chart is empty')
        except Exception:
            error = True
            message = orjson.dumps({'message': 'Cannot parse chart config from answer',
                                    'traceback': "Cannot parse chart config from answer:\n" + res}).decode()

        if error:
            raise SingleMessageError(message)

        save_chart(session=session, chart=orjson.dumps(chart).decode(), record_id=self.record.id)

        return chart

    def check_save_predict_data(self, session: Session, res: str) -> bool:

        json_str = extract_nested_json(res)

        if not json_str:
            json_str = ''

        save_predict_data(session=session, record_id=self.record.id, data=json_str)

        if json_str == '':
            return False

        return True

    def save_error(self, session: Session, message: str):
        return save_error_message(session=session, record_id=self.record.id, message=message)

    def save_sql_data(self, session: Session, data_obj: Dict[str, Any]):
        try:
            data_result = data_obj.get('data')
            # Storage guard, NOT the SQL-generation limit. `enable_sql_row_limit`
            # decides whether the generated SQL carries a LIMIT; gating this cap
            # on it meant that with GENERATE_SQL_QUERY_LIMIT_ENABLED=false (what
            # docker-compose ships) an unbounded result set was serialised whole
            # into a Text column (AUDIT D-16).
            limit = settings.AGENTIC_PERSISTED_ROW_CAP
            if data_result:
                data_result = prepare_for_orjson(data_result)
                if data_result and len(data_result) > limit:
                    data_obj['data'] = data_result[:limit]
                    data_obj['limit'] = limit
                else:
                    data_obj['data'] = data_result
                data_obj['datasource'] = self.ds.id
            return save_sql_exec_data(session=session, record_id=self.record.id,
                                      data=orjson.dumps(data_obj).decode())
        except Exception as e:
            raise e

    def finish(self, session: Session):
        return finish_record(session=session, record_id=self.record.id)

    def execute_sql(self, sql: str):
        """Execute SQL query

        Args:
            ds: Data source instance
            sql: SQL query statement

        Returns:
            Query results
        """
        SQLBotLogUtil.info(f"Executing SQL on ds_id {self.ds.id}: {sql}")
        try:
            return exec_sql(ds=self.ds, sql=sql, origin_column=False)
        except Exception as e:
            if isinstance(e, ParseSQLResultError):
                raise e
            else:
                err = traceback.format_exc(limit=1, chain=True)
                raise SQLBotDBError(err)

    def pop_chunk(self):
        try:
            chunk = self.chunk_list.pop(0)
            return chunk
        except IndexError as e:
            return None

    def await_result(self):
        while self.is_running():
            while True:
                chunk = self.pop_chunk()
                if chunk is not None:
                    yield chunk
                else:
                    break
        while True:
            chunk = self.pop_chunk()
            if chunk is None:
                break
            yield chunk

    def run_task_async(self, in_chat: bool = True, stream: bool = True,
                       finish_step: ChatFinishStep = ChatFinishStep.GENERATE_CHART, return_img: bool = True):
        if in_chat:
            stream = True
        self.future = executor.submit(self.run_task_cache, in_chat, stream, finish_step, return_img)

    def run_task_cache(self, in_chat: bool = True, stream: bool = True,
                       finish_step: ChatFinishStep = ChatFinishStep.GENERATE_CHART, return_img: bool = True):
        for chunk in self.run_task(in_chat, stream, finish_step, return_img):
            self.chunk_list.append(chunk)

    def _row_rag_fallback_table(self, _session, question):
        """Last-resort semantic row retrieval, used only after the SQL pipeline
        fails. Returns a {fields, data} table of grounded rows, or None when
        nothing clears the confidence floor. Never invents content.

        The search is restricted to the tables this caller may read: the bound
        datasource when there is one, otherwise every datasource in the caller's
        workspace. row_embeddings is a single global store shared by all
        workspaces, so an unrestricted search returns other tenants' rows
        (AUDIT D-01). Fails closed — an allow-list we cannot build yields no
        fallback rather than an unfiltered one."""
        from common.core.config import settings
        if not settings.ROW_RAG_ENABLED or not _session:
            return None
        try:
            from apps.datasource.crud.table import get_readable_table_names
            from apps.datasource.row_rag.fallback import row_rag_fallback
            _oid = getattr(self.current_user, 'oid', None) or 1
            _ds_id = self.ds.id if isinstance(self.ds, CoreDatasource) else None
            _allowed = get_readable_table_names(_session, _oid, _ds_id)
            if not _allowed:
                SQLBotLogUtil.info(
                    'row_rag fallback skipped: no readable tables for this caller')
                return None
            table = row_rag_fallback(_session.get_bind(), question, _allowed)
        except Exception:
            traceback.print_exc()
            return None
        if not table or not table.get('data'):
            return None
        return table

    def _emit_table_result(self, _session, question, table, in_chat, stream, json_result):
        """Render a {fields, data} table as a plain table chart AND deliver the
        rows the same way the normal SQL path does: persist them to record.data
        and emit the 'sql-data' signal so the frontend actually loads them.
        (The earlier version skipped both when self.ds was None, so the UI showed
        the chart skeleton with 'No Data'.) No LLM chart call."""
        fields = table.get('fields') or []
        data = table.get('data') or []
        data = DataFormat.convert_large_numbers_in_object_array(data)
        data = DataFormat.normalize_qualified_sql_column_keys_in_object_array(data)
        # Persist rows to the chat record so the frontend (get_chat_chart_data ->
        # record.data) can load them. Does NOT require a datasource, so it also
        # works in the no-datasource-match case (self.ds is None).
        data_obj = {'fields': fields, 'data': data,
                    'datasource': (self.ds.id if self.ds else None)}
        try:
            save_sql_exec_data(session=_session, record_id=self.record.id,
                               data=orjson.dumps(data_obj).decode())
        except Exception:
            traceback.print_exc()
        title = (question or 'Closest matches').strip()[:40]
        chart = {'type': 'table', 'title': title,
                 'columns': [{'name': f, 'value': f} for f in fields]}
        save_chart(session=_session, chart=orjson.dumps(chart).decode(), record_id=self.record.id)
        if in_chat:
            yield 'data:' + orjson.dumps(
                {'content': '',
                 'reasoning_content': '\n[fallback] no SQL match; showing the closest rows '
                                      'by meaning (vector search)\n',
                 'type': 'sql-result'}).decode() + '\n\n'
            # signal the frontend to load the rows we just persisted, THEN render
            yield 'data:' + orjson.dumps({'content': 'execute-success', 'type': 'sql-data'}).decode() + '\n\n'
            yield 'data:' + orjson.dumps(
                {'content': orjson.dumps(chart).decode(), 'type': 'chart'}).decode() + '\n\n'
            yield 'data:' + orjson.dumps({'type': 'finish'}).decode() + '\n\n'
        elif stream:
            if data and fields:
                df = pd.DataFrame(data, columns=fields)
                yield DataFormat.safe_convert_to_string(df).to_markdown(index=False) + '\n\n'
        else:
            json_result['data'] = data
            json_result['chart'] = chart
            json_result['success'] = True
            yield json_result

    def run_task(self, in_chat: bool = True, stream: bool = True,
                 finish_step: ChatFinishStep = ChatFinishStep.GENERATE_CHART, return_img: bool = True):
        json_result: Dict[str, Any] = {'success': True}
        _session = None
        try:
            _session = session_maker()
            if self.ds:
                oid = self.ds.oid if isinstance(self.ds, CoreDatasource) else 1
                ds_id = self.ds.id if isinstance(self.ds, CoreDatasource) else None

                self.filter_terminology_template(_session, oid, ds_id)

                self.filter_training_template(_session, oid, ds_id)

                self.filter_custom_prompts(_session, CustomPromptTypeEnum.GENERATE_SQL, oid, ds_id)

                self.init_messages(_session)

            # return id
            if in_chat:
                yield 'data:' + orjson.dumps({'type': 'id', 'id': self.get_record().id}).decode() + '\n\n'
                if self.get_record().regenerate_record_id:
                    yield 'data:' + orjson.dumps({'type': 'regenerate_record_id',
                                                  'regenerate_record_id': self.get_record().regenerate_record_id}).decode() + '\n\n'
                yield 'data:' + orjson.dumps(
                    {'type': 'question', 'question': self.get_record().question}).decode() + '\n\n'
            else:
                if stream:
                    yield '> ' + self.trans('i18n_chat.record_id_in_mcp') + str(self.get_record().id) + '\n'
                    yield '> ' + self.get_record().question + '\n\n'
            if not stream:
                json_result['record_id'] = self.get_record().id

                # select datasource if datasource is none
            if not self.ds:
                ds_res = self.select_datasource(_session)

                for chunk in ds_res:
                    SQLBotLogUtil.info(chunk)
                    if in_chat:
                        yield 'data:' + orjson.dumps(
                            {'content': chunk.get('content'), 'reasoning_content': chunk.get('reasoning_content'),
                             'type': 'datasource-result'}).decode() + '\n\n'
                if in_chat:
                    yield 'data:' + orjson.dumps({'id': self.ds.id, 'datasource_name': self.ds.name,
                                                  'engine_type': self.ds.type_name or self.ds.type,
                                                  'type': 'datasource'}).decode() + '\n\n'

            else:
                self.validate_history_ds(_session)

            # agentic multi-datasource routing: if the same kind of data lives in
            # several datasources (fanout) or the answer needs facts from several
            # (split), run the primary leg through the normal pipeline and stash the
            # rest for post-chart synthesis (UNION for fanout).
            try:
                _verdict = self.decompose_question(_session)
                _subs = _verdict.get('subs', [])
                if len(_subs) >= 2:
                    self._multi_mode = _verdict.get('mode', 'split')
                    self._secondary_subs = _subs[1:]
                    self.chat_question.question = _subs[0]['question']
                    self.chat_question.logical_plan = ''
                    SQLBotLogUtil.info(
                        f'agentic {self._multi_mode}: primary leg = ' + _subs[0]['question'])
                    if in_chat:
                        _label = ('found in multiple files; will combine across them'
                                  if self._multi_mode == 'fanout'
                                  else 'spans multiple datasources')
                        yield 'data:' + orjson.dumps(
                            {'content': '',
                             'reasoning_content':
                                 f'\n[agentic] {self._multi_mode}: this question is {_label} '
                                 f'({len(_subs)} sources); answering the first, then merging...\n',
                             'type': 'sql-result'}).decode() + '\n\n'
            except Exception:
                traceback.print_exc()

            # check connection
            connected = check_connection(ds=self.ds, trans=None)
            if not connected:
                raise SQLBotDBConnectionError('Connect DB failed')

            # APEX-SQL refinement (logical planning, pruning, profiling) is now
            # invoked inside init_messages() so it runs in both code paths.

            # generate + execute sql, wrapped in the bounded agentic retry loop.
            # attempt 1 behaves exactly like the original pipeline; retries only
            # trigger on execution errors, SQL-parse failures and grader-rejected
            # empty results (never on LLM refusals).
            agentic_on = settings.AGENTIC_SQL_RETRY_ENABLED
            max_attempts = max(1, settings.AGENTIC_SQL_MAX_ATTEMPTS) if agentic_on else 1
            attempt = 0
            fallback_used = False
            refusal_retried = False   # one best-effort nudge per question, max
            retry_feedback: Optional[str] = None
            pending_alt_text: Optional[str] = None
            alt_future: Optional[Future] = None
            consistency_futures: list = []
            sql_was_rewritten = False
            while True:
                attempt += 1
                # per-attempt state: a permission rewrite on an abandoned attempt
                # must not gate self-consistency on the next one
                sql_was_rewritten = False
                try:
                    if pending_alt_text is not None:
                        # replay the parallel alternative candidate (no extra LLM call)
                        full_sql_text = pending_alt_text
                        pending_alt_text = None
                        self.sql_message.append(AIMessage(full_sql_text))
                        self.record = save_sql_answer(session=_session, record_id=self.record.id,
                                                      answer=orjson.dumps({'content': full_sql_text}).decode())
                        if in_chat:
                            yield 'data:' + orjson.dumps(
                                {'content': '',
                                 'reasoning_content': '\n[agentic] trying alternative SQL candidate\n',
                                 'type': 'sql-result'}).decode() + '\n\n'
                    else:
                        if attempt > 1 and alt_future is None:
                            alt_future = self._spawn_alt_candidate()
                        # Execution-based self-consistency: fire the extra
                        # candidates now so they run CONCURRENTLY with the
                        # streamed one. Only on the first attempt — once a retry
                        # is in play the feedback loop is the better lever, and
                        # the candidates would be voting on a stale prompt.
                        if attempt == 1:
                            consistency_futures = self._spawn_consistency_candidates()
                        sql_res = self.generate_sql(_session, feedback=retry_feedback)
                        full_sql_text = ''
                        for chunk in sql_res:
                            full_sql_text += chunk.get('content')
                            if in_chat:
                                yield 'data:' + orjson.dumps(
                                    {'content': chunk.get('content'),
                                     'reasoning_content': chunk.get('reasoning_content'),
                                     'type': 'sql-result'}).decode() + '\n\n'
                    if in_chat:
                        yield 'data:' + orjson.dumps({'type': 'info', 'msg': 'sql generated'}).decode() + '\n\n'
                    # filter sql
                    SQLBotLogUtil.info(full_sql_text)

                    chart_type = self.get_chart_type_from_sql_answer(full_sql_text)

                    # return title (first attempt only)
                    if self.change_title and attempt == 1:
                        llm_brief = self.get_brief_from_sql_answer(full_sql_text)
                        llm_brief_generated = bool(llm_brief)
                        if llm_brief_generated or (
                                self.chat_question.question and self.chat_question.question.strip() != ''):
                            save_brief = llm_brief if (llm_brief and llm_brief != '') else \
                                self.chat_question.question.strip()[:20]
                            brief = rename_chat(session=_session,
                                                rename_object=RenameChat(id=self.get_record().chat_id,
                                                                         brief=save_brief,
                                                                         brief_generate=llm_brief_generated))
                            if in_chat:
                                yield 'data:' + orjson.dumps({'type': 'brief', 'brief': brief}).decode() + '\n\n'
                            if not stream:
                                json_result['title'] = brief

                    use_dynamic_ds: bool = self.current_assistant and self.current_assistant.type in dynamic_ds_types
                    is_page_embedded: bool = self.current_assistant and self.current_assistant.type == 4
                    dynamic_sql_result = None
                    sqlbot_temp_sql_text = None
                    assistant_dynamic_sql = None
                    # row permission

                    sql_operate = OperationEnum.GENERATE_SQL
                    sql, tables = self.check_sql(session=_session, res=full_sql_text, operate=sql_operate)

                    # Deterministic identifier check: every table/column must appear
                    # verbatim in the schema we put in the prompt. Runs before row
                    # permissions and execution so a hallucinated or case-shifted
                    # name is caught with an exact correction rather than surfacing
                    # as an opaque driver error several steps later.
                    if agentic_on and attempt < max_attempts:
                        _id_findings = self.validate_identifiers(sql)
                        if _id_findings:
                            raise _AgenticIdentifierReject(
                                format_identifier_feedback(_id_findings), sql)

                    if ((not self.current_assistant or is_page_embedded) and is_normal_user(
                            self.current_user)) or use_dynamic_ds:
                        sql_result = None

                        if use_dynamic_ds:
                            dynamic_sql_result = self.generate_assistant_dynamic_sql(_session, sql, tables)
                            sqlbot_temp_sql_text = dynamic_sql_result.get(
                                'sqlbot_temp_sql_text') if dynamic_sql_result else None
                        else:
                            sql_result = self.generate_filter(_session, sql, tables)  # maybe no sql and tables

                        if sql_result:
                            SQLBotLogUtil.info(sql_result)
                            sql_operate = OperationEnum.GENERATE_SQL_WITH_PERMISSIONS
                            sql = self.check_save_sql(session=_session, res=sql_result, operate=sql_operate)
                            sql_was_rewritten = True
                        elif dynamic_sql_result and sqlbot_temp_sql_text:
                            sql_operate = OperationEnum.GENERATE_DYNAMIC_SQL
                            assistant_dynamic_sql = self.check_save_sql(session=_session, res=sqlbot_temp_sql_text,
                                                                        operate=sql_operate)
                            sql_was_rewritten = True
                        else:
                            sql = self.check_save_sql(session=_session, res=full_sql_text, operate=sql_operate)
                    else:
                        sql = self.check_save_sql(session=_session, res=full_sql_text, operate=sql_operate)

                    SQLBotLogUtil.info('sql: ' + sql)

                    if not stream:
                        json_result['sql'] = sql

                    format_sql = sqlparse.format(sql, reindent=True)
                    if in_chat:
                        yield 'data:' + orjson.dumps({'content': format_sql, 'type': 'sql'}).decode() + '\n\n'
                    else:
                        if stream:
                            yield f'```sql\n{format_sql}\n```\n\n'

                    # execute sql
                    real_execute_sql = sql
                    if sqlbot_temp_sql_text and assistant_dynamic_sql:
                        dynamic_sql_result.pop('sqlbot_temp_sql_text')
                        for origin_table, subsql in dynamic_sql_result.items():
                            assistant_dynamic_sql = assistant_dynamic_sql.replace(
                                f'{dynamic_subsql_prefix}{origin_table}', subsql)
                        real_execute_sql = assistant_dynamic_sql

                    if finish_step.value <= ChatFinishStep.GENERATE_SQL.value:
                        # Caller wants SQL only, so nothing below executes — including
                        # the consensus vote, which needs execution results. Cancel
                        # the candidates rather than leaving them to burn GPU on a
                        # verdict no one will read.
                        for _f in consistency_futures:
                            _f.cancel()
                        consistency_futures = []
                        if in_chat:
                            yield 'data:' + orjson.dumps({'type': 'finish'}).decode() + '\n\n'
                        if not stream:
                            yield json_result
                        return

                    # For retrieval ("list all X") questions without an explicit
                    # row count, lift an over-small model LIMIT so the complete
                    # result is returned (display is capped downstream). Gated on
                    # is_listing_question: on a superlative/aggregation question
                    # the model's LIMIT 1 IS the answer and must survive.
                    real_execute_sql = self._maybe_raise_limit(real_execute_sql)
                    real_execute_sql = self._apply_nulls_last(real_execute_sql)
                    self.current_logs[OperationEnum.EXECUTE_SQL] = start_log(session=_session,
                                                                             operate=OperationEnum.EXECUTE_SQL,
                                                                             record_id=self.record.id,
                                                                             local_operation=True)
                    result = self.execute_sql(sql=real_execute_sql)
                    self.current_logs[OperationEnum.EXECUTE_SQL] = end_log(session=_session,
                                                                           log=self.current_logs[
                                                                               OperationEnum.EXECUTE_SQL],
                                                                           full_message={'sql': real_execute_sql,
                                                                                         'count': len(
                                                                                             result.get('data'))})

                    # Execution-based self-consistency: execute the parallel
                    # candidates and keep the result a plurality agrees on.
                    #
                    # Skipped when the SQL was rewritten for row permissions or
                    # dynamic datasources: those rewrites are applied to the
                    # primary statement only, so voting in a candidate that never
                    # went through them would execute UNFILTERED SQL for a
                    # permission-limited user. Correctness of the permission
                    # boundary outranks the accuracy gain.
                    if consistency_futures:
                        if sql_was_rewritten:
                            SQLBotLogUtil.info(
                                'self-consistency skipped: sql was rewritten for permissions')
                            for _f in consistency_futures:
                                _f.cancel()
                        else:
                            _winner = self._apply_self_consistency(
                                _session, sql, real_execute_sql, result, consistency_futures)
                            if _winner:
                                sql = _winner['sql']
                                real_execute_sql = _winner['execute_sql']
                                result = _winner['result']
                                self.chat_question.sql = sql
                                save_sql(session=_session, sql=sql, record_id=self.record.id)
                                if not stream:
                                    json_result['sql'] = sql
                                SQLBotLogUtil.info('self-consistency: replaced primary sql with '
                                                   f'{_winner.get("votes")}-vote candidate')
                                if in_chat:
                                    yield 'data:' + orjson.dumps(
                                        {'content': '',
                                         'reasoning_content':
                                             f'\n[agentic] self-consistency: {_winner.get("votes")}/'
                                             f'{_winner.get("total_votes")} candidates agreed on a '
                                             f'different result; using it\n',
                                         'type': 'sql-result'}).decode() + '\n\n'
                                    # The winning SQL is not the one already
                                    # streamed to the UI. The client ASSIGNS on
                                    # type 'sql' (ChartAnswer.vue), so re-emitting
                                    # replaces the displayed statement with the
                                    # one that actually produced these rows.
                                    yield 'data:' + orjson.dumps(
                                        {'content': sqlparse.format(sql, reindent=True),
                                         'type': 'sql'}).decode() + '\n\n'
                                elif stream:
                                    yield f'```sql\n{sqlparse.format(sql, reindent=True)}\n```\n\n'
                        consistency_futures = []

                    # agentic grader: audit suspicious (empty) results while retry
                    # budget remains; non-empty results are accepted without cost
                    if agentic_on and attempt < max_attempts and not result.get('data'):
                        _ok, _reason = self.grade_sql_result(_session, real_execute_sql, result)
                        if not _ok:
                            raise _AgenticGraderReject(_reason or 'empty result rejected by grader', sql)
                    break  # success
                except Exception as agentic_e:
                    _stage = ''
                    _detail = ''
                    _failed_sql = None
                    if isinstance(agentic_e, _AgenticGraderReject):
                        _retryable = True
                        _stage, _detail, _failed_sql = 'result-check', agentic_e.reason, agentic_e.sql
                    elif isinstance(agentic_e, _AgenticIdentifierReject):
                        _retryable = True
                        _stage, _detail, _failed_sql = ('identifier-check', agentic_e.reason,
                                                        agentic_e.sql)
                    elif isinstance(agentic_e, SQLBotDBError):
                        _retryable = True
                        _stage, _detail = 'execute', str(agentic_e)[:settings.LLM_ERROR_TEXT_MAX_CHARS]
                        _failed_sql = locals().get('real_execute_sql') or locals().get('sql')
                    elif isinstance(agentic_e, SingleMessageError) and is_retryable_single_message(str(agentic_e)):
                        _retryable = True
                        if is_refusal_message(str(agentic_e)):
                            # A refusal is not a malformed answer: re-sending the
                            # generic parse feedback would tell the model nothing.
                            # Give it the specific "answer anyway" nudge, and only
                            # once -- a model that refuses twice is surfaced.
                            _stage = 'refusal'
                            _detail = strip_refusal_tag(str(agentic_e))[:settings.LLM_ERROR_TEXT_MAX_CHARS]
                            if refusal_retried:
                                _retryable = False
                            refusal_retried = True
                        else:
                            _stage, _detail = 'parse', str(agentic_e)[:settings.LLM_ERROR_TEXT_MAX_CHARS]
                    else:
                        _retryable = False
                    # The refusal tag is an internal marker; a refusal that ends up
                    # surfacing must reach the user as the model's own wording.
                    if isinstance(agentic_e, SingleMessageError) and is_refusal_message(str(agentic_e)):
                        agentic_e = SingleMessageError(strip_refusal_tag(str(agentic_e)))
                    # This attempt is being abandoned, so its consistency
                    # candidates are voting on a prompt we no longer trust.
                    if consistency_futures:
                        for _f in consistency_futures:
                            _f.cancel()
                        consistency_futures = []
                    if not (agentic_on and _retryable):
                        raise agentic_e   # sanitized above: never leak the internal refusal tag
                    # harvest the parallel alternative candidate, if one is cooking
                    if alt_future is not None:
                        try:
                            _alt_text, _, _ = alt_future.result(timeout=settings.LLM_ALT_CANDIDATE_TIMEOUT)
                        except Exception:
                            _alt_text = ''
                        alt_future = None
                        if _alt_text and extract_nested_json(_alt_text):
                            pending_alt_text = _alt_text
                    if attempt >= max_attempts:
                        if (settings.AGENTIC_DS_FALLBACK_ENABLED and self.ds_auto_selected
                                and not fallback_used and self.ds_candidates
                                and not self._secondary_subs):
                            # last resort: switch to the next ranked datasource
                            fallback_used = True
                            next_ds = self.ds_candidates.pop(0)
                            SQLBotLogUtil.info(
                                f'agentic: falling back to datasource {next_ds.get("id")} ({next_ds.get("name")})')
                            if in_chat:
                                yield 'data:' + orjson.dumps(
                                    {'content': '',
                                     'reasoning_content':
                                         f'\n[agentic] switching to datasource: {next_ds.get("name")}\n',
                                     'type': 'sql-result'}).decode() + '\n\n'
                            self.sql_message = []
                            self.chat_question.sql = ''
                            self._apply_datasource(_session, next_ds['id'])
                            connected = check_connection(ds=self.ds, trans=None)
                            if not connected:
                                raise SQLBotDBConnectionError('Connect DB failed')
                            attempt = max_attempts - 1  # exactly one fresh attempt on the fallback ds
                            retry_feedback = None
                            pending_alt_text = None
                            continue
                        raise agentic_e   # sanitized above: never leak the internal refusal tag
                    retry_feedback = (refusal_retry_feedback(_detail) if _stage == 'refusal'
                                      else format_retry_feedback(_stage, _detail, _failed_sql))
                    SQLBotLogUtil.info(
                        f'agentic retry {attempt}/{max_attempts}: stage={_stage} detail={_detail[:200]}')
                    if in_chat:
                        yield 'data:' + orjson.dumps(
                            {'content': '',
                             'reasoning_content':
                                 f'\n[agentic] attempt {attempt} failed at {_stage}; retrying...\n',
                             'type': 'sql-result'}).decode() + '\n\n'

            # FANOUT: the same entity lives in several datasources. Run each
            # remaining source, then UNION every row into ONE result table (with a
            # 'source' column) so the combined list is the MAIN visible answer.
            if self._multi_mode == 'fanout' and self._secondary_subs and \
                    finish_step.value > ChatFinishStep.GENERATE_SQL.value:
                try:
                    legs_raw = [{'datasource': getattr(self.ds, 'name', '') or 'primary',
                                 'fields': result.get('fields') or [],
                                 'data': result.get('data') or []}]
                    for _sub in self._secondary_subs:
                        if in_chat:
                            yield 'data:' + orjson.dumps(
                                {'content': '',
                                 'reasoning_content':
                                     f'\n[agentic] fanout: querying {_sub.get("question")} ...\n',
                                 'type': 'sql-result'}).decode() + '\n\n'
                        _leg = self._run_secondary_leg(_session, _sub, raw=True)
                        if _leg and _leg.get('data'):
                            legs_raw.append(_leg)
                    if len(legs_raw) >= 2:
                        _union = merge_union(legs_raw)
                        _src_counts = ', '.join(
                            f"{l.get('datasource')}={len(l.get('data') or [])}" for l in legs_raw)
                        SQLBotLogUtil.info(
                            f'agentic fanout union: {len(_union["data"])} rows from [{_src_counts}]')
                        result = _union
                        chart_type = 'table'
                        self._fanout_union = True
                        if in_chat:
                            yield 'data:' + orjson.dumps(
                                {'content': '',
                                 'reasoning_content':
                                     f'\n[agentic] combined {len(legs_raw)} files into '
                                     f'{len(_union["data"])} rows ({_src_counts})\n',
                                 'type': 'sql-result'}).decode() + '\n\n'
                    self._secondary_subs = []  # consumed; skip the text-synthesis block
                except Exception:
                    traceback.print_exc()
                    self._secondary_subs = []

            # EMPTY-RESULT FALLBACK: the SQL ran but matched nothing (e.g. a futile
            # substring keyword filter). Before showing an empty result,
            # try semantic row retrieval and, if confident, present those rows as the
            # answer (reusing the union-table render path below). Never invents data.
            if (not self._fanout_union and not result.get('data')
                    and finish_step.value > ChatFinishStep.GENERATE_SQL.value):
                _rag_table = self._row_rag_fallback_table(_session, self._original_question)
                if _rag_table:
                    result = _rag_table
                    chart_type = 'table'
                    self._fanout_union = True
                    if in_chat:
                        yield 'data:' + orjson.dumps(
                            {'content': '',
                             'reasoning_content': '\n[fallback] SQL returned no rows; showing the '
                                                  'closest rows by meaning (vector search)\n',
                             'type': 'sql-result'}).decode() + '\n\n'

            _data = DataFormat.convert_large_numbers_in_object_array(result.get('data'))
            _data = DataFormat.normalize_qualified_sql_column_keys_in_object_array(_data)
            result["data"] = _data

            self.save_sql_data(session=_session, data_obj=result)
            if in_chat:
                yield 'data:' + orjson.dumps({'content': 'execute-success', 'type': 'sql-data'}).decode() + '\n\n'
            if not stream:
                json_result['data'] = get_chat_chart_data(_session, self.record.id)

            if finish_step.value <= ChatFinishStep.QUERY_DATA.value:
                if stream:
                    if in_chat:
                        yield 'data:' + orjson.dumps({'type': 'finish'}).decode() + '\n\n'
                    else:
                        _column_list = []
                        for field in result.get('fields'):
                            _column_list.append(AxisObj(name=field, value=field))

                        md_data, _fields_list = DataFormat.convert_object_array_for_pandas(_column_list,
                                                                                           result.get('data'))

                        # data, _fields_list, col_formats = self.format_pd_data(_column_list, result.get('data'))

                        if not _data or not _fields_list:
                            yield 'The SQL execution result is empty.\n\n'
                        else:
                            df = pd.DataFrame(_data, columns=_fields_list)
                            df_safe = DataFormat.safe_convert_to_string(df)
                            markdown_table = df_safe.to_markdown(index=False)
                            yield markdown_table + '\n\n'
                else:
                    yield json_result
                return

            # FANOUT union: the result is a synthetic cross-file table, so the
            # schema-driven LLM chart can't describe it. Build a plain table chart
            # directly from the union columns and skip chart generation.
            if self._fanout_union:
                _title = (self.get_brief_from_sql_answer(full_sql_text)
                          or (self._original_question or 'Combined results').strip()[:40])
                chart = {'type': 'table', 'title': _title,
                         'columns': [{'name': f, 'value': f} for f in (result.get('fields') or [])]}
                save_chart(session=_session, chart=orjson.dumps(chart).decode(), record_id=self.record.id)
                if not stream:
                    json_result['chart'] = chart
                if in_chat:
                    yield 'data:' + orjson.dumps(
                        {'content': orjson.dumps(chart).decode(), 'type': 'chart'}).decode() + '\n\n'
                    yield 'data:' + orjson.dumps({'type': 'finish'}).decode() + '\n\n'
                elif stream:
                    _cols = result.get('fields') or []
                    if _data and _cols:
                        df = pd.DataFrame(_data, columns=_cols)
                        yield DataFormat.safe_convert_to_string(df).to_markdown(index=False) + '\n\n'
                else:
                    yield json_result
                return

            # generate chart
            used_tables_schema, used_tables = self.out_ds_instance.get_db_schema(
                self.ds.id, self.chat_question.question, embedding=False,
                table_list=tables) if self.out_ds_instance else get_table_schema(
                session=_session,
                current_user=self.current_user,
                ds=self.ds,
                question=self.chat_question.question,
                embedding=False, table_list=tables)
            SQLBotLogUtil.info('used_tables_schema: \n' + used_tables_schema)
            chart_res = self.generate_chart(_session, chart_type, used_tables_schema)
            full_chart_text = ''
            for chunk in chart_res:
                full_chart_text += chunk.get('content')
                if in_chat:
                    yield 'data:' + orjson.dumps(
                        {'content': chunk.get('content'), 'reasoning_content': chunk.get('reasoning_content'),
                         'type': 'chart-result'}).decode() + '\n\n'
            if in_chat:
                yield 'data:' + orjson.dumps({'type': 'info', 'msg': 'chart generated'}).decode() + '\n\n'

            # filter chart
            SQLBotLogUtil.info(full_chart_text)
            chart = self.check_save_chart(session=_session, res=full_chart_text)
            SQLBotLogUtil.info(chart)

            if not stream:
                json_result['chart'] = chart

            if in_chat:
                yield 'data:' + orjson.dumps(
                    {'content': orjson.dumps(chart).decode(), 'type': 'chart'}).decode() + '\n\n'
            else:
                if stream:
                    md_data, _fields_list = DataFormat.convert_data_fields_for_pandas(chart, result.get('fields'),
                                                                                      result.get('data'))
                    # data, _fields_list, col_formats = self.format_pd_data(_column_list, result.get('data'))

                    if not md_data or not _fields_list:
                        yield 'The SQL execution result is empty.\n\n'
                    else:
                        df = pd.DataFrame(md_data, columns=_fields_list)
                        df_safe = DataFormat.safe_convert_to_string(df)
                        markdown_table = df_safe.to_markdown(index=False)
                        yield markdown_table + '\n\n'

            # agentic cross-datasource synthesis: run the remaining sub-questions
            # on their datasources and stream a merged answer. A failure here can
            # never break the already-delivered primary answer.
            if self._secondary_subs:
                try:
                    # primary leg: capture many rows for fanout so the UNION is complete
                    if self._multi_mode == 'fanout':
                        _primary_preview = build_result_preview(result.get('fields'), result.get('data'),
                                                                max_rows=settings.AGENTIC_LEG_MAX_ROWS, max_chars=12000)
                    else:
                        _primary_preview = build_result_preview(result.get('fields'), result.get('data'))
                    legs = [{'datasource': getattr(self.ds, 'name', '') or '',
                             'question': self.chat_question.question,
                             'sql': sql,
                             'result_preview': _primary_preview}]
                    for _sub in self._secondary_subs:
                        if in_chat:
                            yield 'data:' + orjson.dumps(
                                {'content': '',
                                 'reasoning_content':
                                     f'\n[agentic] querying {self._multi_mode} source: {_sub.get("question")}\n',
                                 'type': 'sql-result'}).decode() + '\n\n'
                        _leg = self._run_secondary_leg(_session, _sub)
                        if _leg:
                            legs.append(_leg)
                    if len(legs) >= 2:
                        _hdr = ('\n\n---\n**Combined answer across all matching files:**\n'
                                if self._multi_mode == 'fanout'
                                else '\n\n---\n**Cross-datasource answer:**\n')
                        if in_chat:
                            yield 'data:' + orjson.dumps(
                                {'content': '', 'reasoning_content': _hdr,
                                 'type': 'sql-result'}).decode() + '\n\n'
                        elif stream:
                            yield _hdr + '\n'
                        _full_synthesis = ''
                        for chunk in self.synthesize_answer(_session, legs):
                            _piece = chunk.get('content') or ''
                            if not _piece:
                                continue
                            _full_synthesis += _piece
                            if in_chat:
                                yield 'data:' + orjson.dumps(
                                    {'content': '', 'reasoning_content': _piece,
                                     'type': 'sql-result'}).decode() + '\n\n'
                            elif stream:
                                yield _piece
                        if not stream:
                            json_result['synthesis'] = _full_synthesis
                        if _full_synthesis and stream and not in_chat:
                            yield '\n\n'
                except Exception:
                    traceback.print_exc()
                finally:
                    self._secondary_subs = []

            if in_chat:
                yield 'data:' + orjson.dumps({'type': 'finish'}).decode() + '\n\n'
            else:
                # generate picture
                try:
                    if chart.get('type') != 'table' and return_img:
                        # yield '### generated chart picture\n\n'
                        self.current_logs[OperationEnum.GENERATE_PICTURE] = start_log(session=_session,
                                                                                      operate=OperationEnum.GENERATE_PICTURE,
                                                                                      record_id=self.record.id,
                                                                                      local_operation=True)
                        image_url, error = request_picture(self.record.chat_id, self.record.id, chart,
                                                           format_json_data(result))
                        SQLBotLogUtil.info(image_url)
                        if stream:
                            yield f'![{chart.get("type")}]({image_url})'
                        else:
                            json_result['image_url'] = image_url
                        if error is not None:
                            raise error

                        self.current_logs[OperationEnum.GENERATE_PICTURE] = end_log(session=_session,
                                                                                    log=self.current_logs[
                                                                                        OperationEnum.GENERATE_PICTURE],
                                                                                    full_message=image_url)
                except Exception as e:
                    if stream:
                        if chart.get('type') != 'table':
                            yield 'generate or fetch chart picture error.\n\n'
                        raise e

            if not stream:
                yield json_result

        except Exception as e:
            traceback.print_exc()
            # Last resort before surfacing the error (no datasource matched, or the
            # SQL retry loop exhausted): try semantic row retrieval. It only emits
            # when retrieval is confident; otherwise we fall through to the normal
            # error message. It never invents an answer.
            _rag_table = self._row_rag_fallback_table(_session, self._original_question)
            if _rag_table:
                try:
                    yield from self._emit_table_result(
                        _session, self._original_question, _rag_table,
                        in_chat, stream, json_result)
                    return
                except Exception:
                    traceback.print_exc()
            error_msg: str
            if isinstance(e, SingleMessageError):
                error_msg = str(e)
            elif isinstance(e, SQLBotDBConnectionError):
                error_msg = orjson.dumps(
                    {'message': str(e), 'type': 'db-connection-err'}).decode()
            elif isinstance(e, SQLBotDBError):
                error_msg = orjson.dumps(
                    {'message': 'Execute SQL Failed', 'traceback': str(e), 'type': 'exec-sql-err'}).decode()
            else:
                error_msg = orjson.dumps({'message': str(e), 'traceback': traceback.format_exc(limit=1)}).decode()
            if _session:
                self.save_error(session=_session, message=error_msg)
            if in_chat:
                yield 'data:' + orjson.dumps({'content': error_msg, 'type': 'error'}).decode() + '\n\n'
            else:
                if stream:
                    yield f'&#x274c; **ERROR:**\n'
                    yield f'> {error_msg}\n'
                else:
                    json_result['success'] = False
                    json_result['message'] = error_msg
                    yield json_result
        finally:
            self.finish(_session)
            session_maker.remove()

    def run_recommend_questions_task_async(self):
        self.future = executor.submit(self.run_recommend_questions_task_cache)

    def run_recommend_questions_task_cache(self):
        for chunk in self.run_recommend_questions_task():
            self.chunk_list.append(chunk)

    def run_recommend_questions_task(self):
        try:
            _session = session_maker()
            res = self.generate_recommend_questions_task(_session)

            for chunk in res:
                if chunk.get('recommended_question'):
                    yield 'data:' + orjson.dumps(
                        {'content': chunk.get('recommended_question'),
                         'type': 'recommended_question'}).decode() + '\n\n'
                else:
                    yield 'data:' + orjson.dumps(
                        {'content': chunk.get('content'), 'reasoning_content': chunk.get('reasoning_content'),
                         'type': 'recommended_question_result'}).decode() + '\n\n'
        except Exception:
            traceback.print_exc()
        finally:
            session_maker.remove()

    def run_analysis_or_predict_task_async(self, session: Session, action_type: str, base_record: ChatRecord,
                                           in_chat: bool = True, stream: bool = True):
        self.set_record(save_analysis_predict_record(session, base_record, action_type))
        self.future = executor.submit(self.run_analysis_or_predict_task_cache, action_type, in_chat, stream)

    def run_analysis_or_predict_task_cache(self, action_type: str, in_chat: bool = True, stream: bool = True):
        for chunk in self.run_analysis_or_predict_task(action_type, in_chat, stream):
            self.chunk_list.append(chunk)

    def run_analysis_or_predict_task(self, action_type: str, in_chat: bool = True, stream: bool = True):
        json_result: Dict[str, Any] = {'success': True}
        _session = None
        try:
            _session = session_maker()
            if in_chat:
                yield 'data:' + orjson.dumps({'type': 'id', 'id': self.get_record().id}).decode() + '\n\n'
            else:
                if stream:
                    yield '> ' + self.trans('i18n_chat.record_id_in_mcp') + str(self.get_record().id) + '\n'
                    yield '> ' + self.get_record().question + '\n\n'
            if not stream:
                json_result['record_id'] = self.get_record().id

            if action_type == 'analysis':
                # generate analysis
                analysis_res = self.generate_analysis(_session)
                full_text = ''
                for chunk in analysis_res:
                    full_text += chunk.get('content')
                    if in_chat:
                        yield 'data:' + orjson.dumps(
                            {'content': chunk.get('content'), 'reasoning_content': chunk.get('reasoning_content'),
                             'type': 'analysis-result'}).decode() + '\n\n'
                    else:
                        if stream:
                            yield chunk.get('content')
                if in_chat:
                    yield 'data:' + orjson.dumps({'type': 'info', 'msg': 'analysis generated'}).decode() + '\n\n'
                    yield 'data:' + orjson.dumps({'type': 'analysis_finish'}).decode() + '\n\n'
                else:
                    if stream:
                        yield '\n\n'
                if not stream:
                    json_result['content'] = full_text

            elif action_type == 'predict':
                # generate predict
                analysis_res = self.generate_predict(_session)
                full_text = ''
                for chunk in analysis_res:
                    full_text += chunk.get('content')
                    if in_chat:
                        yield 'data:' + orjson.dumps(
                            {'content': chunk.get('content'), 'reasoning_content': chunk.get('reasoning_content'),
                             'type': 'predict-result'}).decode() + '\n\n'
                if in_chat:
                    yield 'data:' + orjson.dumps({'type': 'info', 'msg': 'predict generated'}).decode() + '\n\n'

                has_data = self.check_save_predict_data(session=_session, res=full_text)
                if has_data:
                    if in_chat:
                        yield 'data:' + orjson.dumps({'type': 'predict-success'}).decode() + '\n\n'
                    else:
                        chart = get_chat_chart_config(_session, self.record.id)
                        origin_data = get_chat_chart_data(_session, self.record.id)
                        predict_data = get_chat_predict_data(_session, self.record.id)

                        if stream:
                            md_data, _fields_list = DataFormat.convert_data_fields_for_pandas(chart,
                                                                                              origin_data.get('fields'),
                                                                                              predict_data)
                            if not md_data or not _fields_list:
                                yield 'Predict data result is empty.\n\n'
                            else:
                                df = pd.DataFrame(md_data, columns=_fields_list)
                                df_safe = DataFormat.safe_convert_to_string(df)
                                markdown_table = df_safe.to_markdown(index=False)
                                yield markdown_table + '\n\n'

                        else:
                            json_result['origin_data'] = origin_data
                            json_result['predict_data'] = predict_data

                        # generate picture
                        try:
                            if chart.get('type') != 'table':
                                # yield '### generated chart picture\n\n'

                                _data = get_chat_chart_data(_session, self.record.id)
                                _data['data'] = _data.get('data') + predict_data

                                image_url, error = request_picture(self.record.chat_id, self.record.id, chart,
                                                                   format_json_data(_data))
                                SQLBotLogUtil.info(image_url)
                                if stream:
                                    yield f'![{chart.get("type")}]({image_url})'
                                else:
                                    json_result['image_url'] = image_url
                                if error is not None:
                                    raise error
                        except Exception as e:
                            if stream:
                                if chart.get('type') != 'table':
                                    yield 'generate or fetch chart picture error.\n\n'
                                raise e
                else:
                    if in_chat:
                        yield 'data:' + orjson.dumps({'type': 'predict-failed'}).decode() + '\n\n'
                    else:
                        if stream:
                            yield full_text + '\n\n'
                    if not stream:
                        json_result['success'] = False
                        json_result['message'] = full_text
                if in_chat:
                    yield 'data:' + orjson.dumps({'type': 'predict_finish'}).decode() + '\n\n'

            self.finish(_session)

            if not stream:
                yield json_result
        except Exception as e:
            traceback.print_exc()
            error_msg: str
            if isinstance(e, SingleMessageError):
                error_msg = str(e)
            else:
                error_msg = orjson.dumps({'message': str(e), 'traceback': traceback.format_exc(limit=1)}).decode()
            if _session:
                self.save_error(session=_session, message=error_msg)
            if in_chat:
                yield 'data:' + orjson.dumps({'content': error_msg, 'type': 'error'}).decode() + '\n\n'
            else:
                if stream:
                    yield f'&#x274c; **ERROR:**\n'
                    yield f'> {error_msg}\n'
                else:
                    json_result['success'] = False
                    json_result['message'] = error_msg
                    yield json_result
        finally:
            # end
            session_maker.remove()

    def validate_history_ds(self, session: Session):
        _ds = self.ds
        if not self.current_assistant or self.current_assistant.type == 4:
            try:
                current_ds = session.get(CoreDatasource, _ds.id)
                if not current_ds:
                    raise SingleMessageError('chat.ds_is_invalid')
            except Exception as e:
                raise SingleMessageError("chat.ds_is_invalid")
        else:
            try:
                _ds_list: list[dict] = get_assistant_ds(session=session, llm_service=self)
                match_ds = any(item.get("id") == _ds.id for item in _ds_list)
                if not match_ds:
                    type = self.current_assistant.type
                    msg = f"[please check ds list and public ds list]" if type == 0 else f"[please check ds api]"
                    raise SingleMessageError(msg)
            except Exception as e:
                raise SingleMessageError(f"ds is invalid [{str(e)}]")


def execute_sql_with_db(db: SQLDatabase, sql: str) -> str:
    """Execute SQL query using SQLDatabase

    Args:
        db: SQLDatabase instance
        sql: SQL query statement

    Returns:
        str: Query results formatted as string
    """
    try:
        # Execute query
        result = db.run(sql)

        if not result:
            return "Query executed successfully but returned no results."

        # Format results
        return str(result)

    except Exception as e:
        error_msg = f"SQL execution failed: {str(e)}"
        SQLBotLogUtil.exception(error_msg)
        raise RuntimeError(error_msg)


def request_picture(chat_id: int, record_id: int, chart: dict, data: dict):
    file_name = f'c_{chat_id}_r_{record_id}'

    columns = chart.get('columns') if chart.get('columns') else []
    x = None
    y = None
    series = None
    multi_quota_fields = []
    multi_quota_name = None

    if chart.get('axis'):
        axis_data = chart.get('axis')
        x = axis_data.get('x')
        y = axis_data.get('y')
        series = axis_data.get('series')
        # 获取multi-quota字段列表
        if axis_data.get('multi-quota') and 'value' in axis_data.get('multi-quota'):
            multi_quota_fields = axis_data.get('multi-quota').get('value', [])
            multi_quota_name = axis_data.get('multi-quota').get('name')

    axis = []
    for v in columns:
        axis.append({'name': v.get('name'), 'value': v.get('value')})
    if x:
        axis.append({'name': x.get('name'), 'value': x.get('value'), 'type': 'x'})
    if y:
        y_list = y if isinstance(y, list) else [y]

        for y_item in y_list:
            if isinstance(y_item, dict) and 'value' in y_item:
                y_obj = {
                    'name': y_item.get('name'),
                    'value': y_item.get('value'),
                    'type': 'y'
                }
                # 如果是multi-quota字段，添加标志
                if y_item.get('value') in multi_quota_fields:
                    y_obj['multi-quota'] = True
                axis.append(y_obj)
    if series:
        axis.append({'name': series.get('name'), 'value': series.get('value'), 'type': 'series'})
    if multi_quota_name:
        axis.append({'name': multi_quota_name, 'value': multi_quota_name, 'type': 'other-info'})

    request_obj = {
        "path": os.path.join(settings.MCP_IMAGE_PATH, file_name),
        "type": chart.get('type'),
        "data": orjson.dumps(data.get('data') if data.get('data') else []).decode(),
        "axis": orjson.dumps(axis).decode(),
    }

    _error = None
    try:
        requests.post(url=settings.MCP_IMAGE_HOST, json=request_obj, timeout=settings.SERVER_IMAGE_TIMEOUT)
    except Exception as e:
        _error = e

    request_path = urllib.parse.urljoin(settings.SERVER_IMAGE_HOST, f"{file_name}.png")

    return request_path, _error


def get_token_usage(chunk: BaseMessageChunk, token_usage: dict = None):
    try:
        if chunk.usage_metadata:
            if token_usage is None:
                token_usage = {}
            token_usage['input_tokens'] = chunk.usage_metadata.get('input_tokens')
            token_usage['output_tokens'] = chunk.usage_metadata.get('output_tokens')
            token_usage['total_tokens'] = chunk.usage_metadata.get('total_tokens')
    except Exception:
        pass


def process_stream(res: Iterator[BaseMessageChunk],
                   token_usage: Dict[str, Any] = None,
                   enable_tag_parsing: bool = settings.PARSE_REASONING_BLOCK_ENABLED,
                   start_tag: str = settings.DEFAULT_REASONING_CONTENT_START,
                   end_tag: str = settings.DEFAULT_REASONING_CONTENT_END
                   ):
    if token_usage is None:
        token_usage = {}
    in_thinking_block = False  # 标记是否在思考过程块中
    current_thinking = ''  # 当前收集的思考过程内容
    pending_start_tag = ''  # 用于缓存可能被截断的开始标签部分

    for chunk in res:
        SQLBotLogUtil.info(chunk)
        reasoning_content_chunk = ''
        content = chunk.content
        output_content = ''  # 实际要输出的内容

        # 检查additional_kwargs中的reasoning_content
        if 'reasoning_content' in chunk.additional_kwargs:
            reasoning_content = chunk.additional_kwargs.get('reasoning_content', '')
            if reasoning_content is None:
                reasoning_content = ''

            # 累积additional_kwargs中的思考内容到current_thinking
            current_thinking += reasoning_content
            reasoning_content_chunk = reasoning_content

        # 只有当current_thinking不是空字符串时才跳过标签解析
        if not in_thinking_block and current_thinking.strip() != '':
            output_content = content  # 正常输出content
            yield {
                'content': output_content,
                'reasoning_content': reasoning_content_chunk
            }
            get_token_usage(chunk, token_usage)
            continue  # 跳过后续的标签解析逻辑

        # 如果没有有效的思考内容，并且启用了标签解析，才执行标签解析逻辑
        # 如果有缓存的开始标签部分，先拼接当前内容
        if pending_start_tag:
            content = pending_start_tag + content
            pending_start_tag = ''

        # 检查是否开始思考过程块（处理可能被截断的开始标签）
        if enable_tag_parsing and not in_thinking_block and start_tag:
            if start_tag in content:
                start_idx = content.index(start_tag)
                # 只有当开始标签前面没有其他文本时才认为是真正的思考块开始
                if start_idx == 0 or content[:start_idx].strip() == '':
                    # 完整标签存在且前面没有其他文本
                    output_content += content[:start_idx]  # 输出开始标签之前的内容
                    content = content[start_idx + len(start_tag):]  # 移除开始标签
                    in_thinking_block = True
                else:
                    # 开始标签前面有其他文本，不认为是思考块开始
                    output_content += content
                    content = ''
            else:
                # 检查是否可能有部分开始标签
                for i in range(1, len(start_tag)):
                    if content.endswith(start_tag[:i]):
                        # 只有当当前内容全是空白时才缓存部分标签
                        if content[:-i].strip() == '':
                            pending_start_tag = start_tag[:i]
                            content = content[:-i]  # 移除可能的部分标签
                            output_content += content
                            content = ''
                        break

        # 处理思考块内容
        if enable_tag_parsing and in_thinking_block and end_tag:
            if end_tag in content:
                # 找到结束标签
                end_idx = content.index(end_tag)
                current_thinking += content[:end_idx]  # 收集思考内容
                reasoning_content_chunk += current_thinking  # 添加到当前块的思考内容
                content = content[end_idx + len(end_tag):]  # 移除结束标签后的内容
                current_thinking = ''  # 重置当前思考内容
                in_thinking_block = False
                output_content += content  # 输出结束标签之后的内容
            else:
                # 在遇到结束标签前，持续收集思考内容
                current_thinking += content
                reasoning_content_chunk += content
                content = ''

        else:
            # 不在思考块中或标签解析未启用，正常输出
            output_content += content

        yield {
            'content': output_content,
            'reasoning_content': reasoning_content_chunk
        }
        get_token_usage(chunk, token_usage)


def get_lang_name(lang: str):
    if not lang:
        return '简体中文'
    normalized = lang.lower()
    if normalized.startswith('zh-tw'):
        return '繁体中文'
    if normalized.startswith('en'):
        return '英文'
    if normalized.startswith('ko'):
        return '韩语'
    return '简体中文'


def get_last_conversation_rounds(messages, rounds=settings.GENERATE_SQL_QUERY_HISTORY_ROUND_COUNT):
    """获取最后N轮对话，处理不完整对话的情况"""
    if not messages or rounds <= 0:
        return []

    # 找到所有用户消息的位置
    human_indices = []
    for index, msg in enumerate(messages):
        if msg.get('type') == 'human':
            human_indices.append(index)

    # 如果没有用户消息，返回空
    if not human_indices:
        return []

    # 计算从哪个索引开始
    if len(human_indices) <= rounds:
        # 如果用户消息数少于等于需要的轮数，从第一个用户消息开始
        start_index = human_indices[0]
    else:
        # 否则，从倒数第N个用户消息开始
        start_index = human_indices[-rounds]

    return messages[start_index:]
