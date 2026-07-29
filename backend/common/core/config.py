import secrets
import urllib.parse
from typing import Annotated, Any, Literal

from pydantic import (
    AnyUrl,
    BeforeValidator,
    PostgresDsn,
    computed_field,
    field_validator
)
from pydantic_core import MultiHostUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_cors(v: Any) -> list[str] | str:
    if isinstance(v, str) and not v.startswith("["):
        return [i.strip() for i in v.split(",")]
    elif isinstance(v, list | str):
        return v
    raise ValueError(v)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Use top level .env file (one level above ./backend/)
        env_file="../.env",
        env_ignore_empty=True,
        extra="ignore",
    )
    PROJECT_NAME: str = "SQLBot"
    #CONTEXT_PATH: str = "/sqlbot"
    CONTEXT_PATH: str = ""
    SECRET_KEY: str = secrets.token_urlsafe(32)
    # 60 minutes * 24 hours * 8 days = 8 days
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 8
    FRONTEND_HOST: str = "http://localhost:5173"

    BACKEND_CORS_ORIGINS: Annotated[
        list[AnyUrl] | str, BeforeValidator(parse_cors)
    ] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def all_cors_origins(self) -> list[str]:
        return [str(origin).rstrip("/") for origin in self.BACKEND_CORS_ORIGINS] + [
            self.FRONTEND_HOST
        ]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def API_V1_STR(self) -> str:
        return self.CONTEXT_PATH + "/api/v1"

    POSTGRES_SERVER: str = 'localhost'
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = 'root'
    POSTGRES_PASSWORD: str = "Password123@pg"
    POSTGRES_DB: str = "sqlbot"
    SQLBOT_DB_URL: str = ''
    # SQLBOT_DB_URL: str = 'mysql+pymysql://root:Password123%40mysql@127.0.0.1:3306/sqlbot'

    TOKEN_KEY: str = "X-SQLBOT-TOKEN"
    DEFAULT_PWD: str = "SQLBot@123456"
    ASSISTANT_TOKEN_KEY: str = "X-SQLBOT-ASSISTANT-TOKEN"

    CACHE_TYPE: Literal["redis", "memory", "None"] = "memory"
    CACHE_REDIS_URL: str | None = None  # Redis URL, e.g., "redis://[[username]:[password]]@localhost:6379/0"

    LOG_LEVEL: str = "INFO"  # DEBUG, INFO, WARNING, ERROR
    LOG_DIR: str = "logs"
    LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s:%(lineno)d - %(message)s"
    SQL_DEBUG: bool = False
    BASE_DIR: str = "/opt/sqlbot"
    SCRIPT_DIR: str = f"{BASE_DIR}/scripts"
    UPLOAD_DIR: str = "/opt/sqlbot/data/file"
    SQLBOT_KEY_EXPIRED: int = 100  # License key expiration timestamp, 0 means no expiration

    @computed_field  # type: ignore[prop-decorator]
    @property
    def SQLALCHEMY_DATABASE_URI(self) -> PostgresDsn | str:
        if self.SQLBOT_DB_URL:
            return self.SQLBOT_DB_URL
        # return MultiHostUrl.build(
        #     scheme="postgresql+psycopg",
        #     username=urllib.parse.quote(self.POSTGRES_USER),
        #     password=urllib.parse.quote(self.POSTGRES_PASSWORD),
        #     host=self.POSTGRES_SERVER,
        #     port=self.POSTGRES_PORT,
        #     path=self.POSTGRES_DB,
        # )
        return f"postgresql+psycopg://{urllib.parse.quote(self.POSTGRES_USER)}:{urllib.parse.quote(self.POSTGRES_PASSWORD)}@{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    MCP_IMAGE_PATH: str = '/opt/sqlbot/images'
    EXCEL_PATH: str = '/opt/sqlbot/data/excel'
    MCP_IMAGE_HOST: str = 'http://localhost:3000'
    SERVER_IMAGE_HOST: str = 'http://YOUR_SERVE_IP:MCP_PORT/images/'
    SERVER_IMAGE_TIMEOUT: int = 15

    LOCAL_MODEL_PATH: str = '/opt/sqlbot/models'
    DEFAULT_EMBEDDING_MODEL: str = 'shibing624/text2vec-base-chinese'
    EMBEDDING_ENABLED: bool = True
    EMBEDDING_DEFAULT_SIMILARITY: float = 0.4
    EMBEDDING_TERMINOLOGY_SIMILARITY: float = EMBEDDING_DEFAULT_SIMILARITY
    EMBEDDING_DATA_TRAINING_SIMILARITY: float = EMBEDDING_DEFAULT_SIMILARITY
    EMBEDDING_DEFAULT_TOP_COUNT: int = 5
    EMBEDDING_TERMINOLOGY_TOP_COUNT: int = EMBEDDING_DEFAULT_TOP_COUNT
    EMBEDDING_DATA_TRAINING_TOP_COUNT: int = EMBEDDING_DEFAULT_TOP_COUNT

    # External OpenAI-compatible embedding endpoint (e.g. Ollama). When both
    # EMBEDDING_API_BASE and EMBEDDING_API_MODEL are set, embeddings are routed
    # there instead of the bundled local model. Empty -> bundled local model.
    EMBEDDING_API_BASE: str = ''
    EMBEDDING_API_MODEL: str = ''
    EMBEDDING_API_KEY: str = ''

    # 是否启用SQL查询行数限制，默认值，可被参数配置覆盖
    GENERATE_SQL_QUERY_LIMIT_ENABLED: bool = True
    GENERATE_SQL_QUERY_HISTORY_ROUND_COUNT: int = 3

    PARSE_REASONING_BLOCK_ENABLED: bool = True
    DEFAULT_REASONING_CONTENT_START: str = '<think>'
    DEFAULT_REASONING_CONTENT_END: str = '</think>'

    PG_POOL_SIZE: int = 20
    PG_MAX_OVERFLOW: int = 30
    PG_POOL_RECYCLE: int = 3600
    PG_POOL_PRE_PING: bool = True

    TABLE_EMBEDDING_ENABLED: bool = True
    TABLE_EMBEDDING_COUNT: int = 10
    # Minimum question<->table cosine for a table to enter the prompt, applied
    # after the top-COUNT cut. 0 disables the floor (pre-2026-07 behaviour: always
    # exactly COUNT tables, however irrelevant). The highest-scoring table is
    # always kept, so this can never produce an empty schema.
    TABLE_EMBEDDING_COSINE_FLOOR: float = 0.0
    DS_EMBEDDING_COUNT: int = 10

    # --- Agentic pipeline (retry loop, grader, hybrid ranking, decomposition) ---
    AGENTIC_SQL_RETRY_ENABLED: bool = True
    AGENTIC_SQL_MAX_ATTEMPTS: int = 3
    AGENTIC_GRADER_ENABLED: bool = True
    AGENTIC_MULTI_CANDIDATE_ENABLED: bool = True
    AGENTIC_DS_FALLBACK_ENABLED: bool = True
    AGENTIC_DECOMPOSE_ENABLED: bool = True
    AGENTIC_HYBRID_RANKING_ENABLED: bool = True
    # Deterministic pre-execution check that every table/column in the generated
    # SQL appears verbatim in the prompt's schema. Costs no LLM call and turns an
    # opaque driver error into a precise retry hint. Fails open on any doubt.
    AGENTIC_IDENTIFIER_CHECK_ENABLED: bool = True
    # Execution-based self-consistency: generate N candidates, execute each, and
    # keep the result the plurality agrees on (survey: MBR-Exec / C3 / MCS-SQL).
    # OFF by default — it costs (N-1) extra LLM calls on EVERY question, which is
    # the wrong trade on a slow local GPU. Turn on when accuracy outranks latency.
    AGENTIC_SELF_CONSISTENCY_ENABLED: bool = False
    AGENTIC_SELF_CONSISTENCY_N: int = 3          # total candidates incl. the first
    AGENTIC_SELF_CONSISTENCY_TIMEOUT: int = 180  # seconds to wait for the extra ones

    # --- Value linking (CHESS-style cell retrieval, PRE-generation) ------------
    # Literals in a question ("Paid", "R&D", "GreenGrid Energy") are otherwise
    # guessed from column names alone. This retrieves the actual matching cell
    # values from the selected tables and shows them to the model BEFORE it
    # writes the WHERE clause. Costs bounded read-only SQL, no LLM call.
    VALUE_LINKING_ENABLED: bool = True
    VALUE_LINKING_MAX_TABLES: int = 5       # tables probed per question
    VALUE_LINKING_MAX_COLUMNS: int = 12     # text columns probed per table
    VALUE_LINKING_DISTINCT_LIMIT: int = 200 # distinct values fetched per column
    VALUE_LINKING_MAX_HINTS: int = 20       # value hints injected into the prompt
    VALUE_LINKING_MIN_SCORE: float = 0.72   # similarity floor for a hint
    VALUE_LINKING_CACHE_TTL: int = 300      # seconds to cache a column's values

    # --- Skeleton-masked few-shot selection (DAIL-SQL) ------------------------
    # Rank SQL examples by masked-question (skeleton) similarity fused with the
    # raw embedding order, instead of raw question cosine alone. Pure re-ranking
    # over already-retrieved rows: no re-embedding, no migration, no LLM call.
    SKELETON_FEWSHOT_ENABLED: bool = True
    SKELETON_FEWSHOT_POOL: int = 30         # candidates pulled before re-ranking
    # APEX-SQL refinement (logical plan + dual-pathway prune + data profiling probes).
    # This is the single biggest latency cost per question (many sequential LLM calls
    # on the local model). Set APEX_ENABLED=false to trade refinement quality for a
    # large speed-up (questions then fail fast and reach the RAG fallback sooner).
    APEX_ENABLED: bool = True

    # --- Agentic tuning (no hardcoded magic numbers; all overridable via env) ---
    # --- Table sampling shown to the model (shape-adaptive, domain-agnostic) ---
    # Sampling adapts to each table instead of using fixed row/column counts: it
    # probes PROBE_ROWS rows, emits as many as fit CHAR_BUDGET, and summarises
    # every column's distinct values. A column whose values fit inside
    # DISTINCT_PER_COL is reported as a closed set ("one of: ..."), which is what
    # makes pivoted sheets answerable — a "Metric" column holding "Net Profit" is
    # then visible as a filterable value no matter how deep that row sits, in any
    # domain (status, region, category, account code, ...).
    TABLE_SAMPLE_PROBE_ROWS: int = 50
    TABLE_SAMPLE_CHAR_BUDGET: int = 3000
    TABLE_SAMPLE_DISTINCT_PER_COL: int = 25
    TABLE_SAMPLE_VALUE_MAXLEN: int = 100
    # "list all X" completeness: lift an over-small model LIMIT up to this cap.
    AGENTIC_FULL_RESULT_LIMIT: int = 1000
    # Fanout co-relevance: a datasource is a fanout candidate when its cosine is
    # >= max(FLOOR, primary_cosine - MARGIN). Tune per embedding model.
    AGENTIC_FANOUT_COSINE_FLOOR: float = 0.40
    AGENTIC_FANOUT_COSINE_MARGIN: float = 0.20
    AGENTIC_FANOUT_MAX_SOURCES: int = 5          # incl. the primary
    AGENTIC_LEG_MAX_ROWS: int = 300              # rows captured per fanout leg
    AGENTIC_LEG_EMPTY_RETRIES: int = 2           # extra retries when a leg is empty
    # Retrieval / aggregation detection keywords (comma-separated, multilingual).
    # Extend for your language without code changes.
    AGENTIC_LISTING_KEYWORDS: str = (
        'list,show,display,find,all,every,each,get,give,'
        '列出,显示,展示,查找,所有,全部,每个,列表,'
        '목록,모두,모든,전체,보여,표시')
    AGENTIC_AGGREGATION_KEYWORDS: str = (
        'sum,total,average,avg,how many,number of,count,max,min,highest,lowest,trend,growth,'
        '总和,总计,合计,平均,多少,数量,计数,最大,最小,最高,最低,趋势,增长,'
        '합계,평균,총,개수,최대,최소')

    # --- Excel island detection (multi-table-per-sheet) ---------------------
    EXCEL_ISLAND_DETECTION_ENABLED: bool = True   # off => exact pre-island behavior
    # Split side-by-side tables (same rows, separated by a fully-empty column)
    # into separate tables. On by default so two tables placed next to each other
    # are detected with their own headers. Set False only if your sheets contain
    # single tables with a genuinely empty spacer column in the middle (rare).
    EXCEL_SPLIT_SIDE_BY_SIDE: bool = True
    EXCEL_ISLAND_MIN_ROWS: int = 1
    # --- Content-aware datasource routing (sampled cell values in embedding) ---
    EMBEDDING_SAMPLE_ENABLED: bool = True
    EMBEDDING_SAMPLE_ROWS: int = 50          # rows scanned per table (one query)
    EMBEDDING_SAMPLE_VALUES_PER_COL: int = 10
    EMBEDDING_SAMPLE_VALUE_MAXLEN: int = 100
    EMBEDDING_SAMPLE_TOTAL_BUDGET: int = 1500  # keep schema+samples+description within the embedding model's context
    # --- PostgreSQL full-text search for Excel datasources ---
    EXCEL_FTS_ENABLED: bool = True
    # --- Parallel/repeated-column UNION guidance (tables that repeat a column for parallel roles) ---
    PARALLEL_COLUMNS_HINT_ENABLED: bool = True
    # --- LLM header-row fallback: only fires when the offline heuristic is unsure ---
    # --- Excel header-row detection heuristic ------------------------------
    # Previously module constants in apps/datasource/utils/header_detection.py,
    # so tuning them for a customer's spreadsheet conventions needed a code
    # change and a rebuild. The weights are a scoring blend and should sum to
    # ~1.0; HEADER_DETECT_CONFIDENCE is what the LLM fallback triggers below.
    HEADER_DETECT_ROWS: int = 25             # leading rows sampled when detecting
    HEADER_DETECT_DATA_SAMPLE: int = 20      # rows below a candidate used to profile data
    HEADER_DETECT_MIN_FILL_RATIO: float = 0.6   # a header must fill this fraction of columns
    HEADER_DETECT_CONFIDENCE: float = 0.62   # below this the heuristic is "unsure"
    HEADER_W_FILL: float = 0.30              # header rows are densely filled
    HEADER_W_UNIQUE: float = 0.16            # header labels are mostly distinct
    HEADER_W_STRING: float = 0.14            # header cells are text, not numbers/dates
    HEADER_W_DIVERGENCE: float = 0.20        # header text sits on top of typed data
    HEADER_W_BELOW: float = 0.05             # rows underneath should contain data
    HEADER_W_BREVITY: float = 0.15           # header cells are shorter than the data
    HEADER_W_POSITION: float = 0.15          # multiplicative prior: real headers sit near the top

    HEADER_LLM_ENABLED: bool = False              # opt-in (adds a small LLM call at ingest)
    HEADER_LLM_TRIGGER_CONF: float = 0.62         # run the LLM when heuristic confidence < this
    HEADER_LLM_MODEL: str = ""                    # '' = default chat model; set a SMALL model, e.g. 'llama3.2:latest'
    HEADER_LLM_MAX_ROWS: int = 15                 # rows of the sheet shown to the LLM
    # --- Auto-generated datasource summary (finder description) ---
    DS_SUMMARY_ENABLED: bool = True
    DS_SUMMARY_MAXLEN: int = 500   # must fit core_datasource.description varchar(512)

    # --- Row-level vector RAG fallback ---
    ROW_RAG_ENABLED: bool = False            # master on/off (ingest + fallback)
    ROW_RAG_EMBED_BATCH: int = 64            # rows per embedding batch at import
    ROW_RAG_TOP_K: int = 10                  # candidate rows pulled from pgvector
    ROW_RAG_MIN_COSINE: float = 0.5          # confidence floor; below -> "no match"
    ROW_RAG_EMBED_DIM: int = 1024            # mxbai-embed-large dimension
    ROW_RAG_CONTENT_MAX_CHARS: int = 4000    # cap per-row content_text length
    ROW_RAG_REL_MARGIN: float = 0.06         # also drop rows >this far below the top hit
                                             # (keeps the relevant cluster, cuts cross-file noise)

    ORACLE_CLIENT_PATH: str = '/opt/sqlbot/db_client/oracle_instant_client'

    @field_validator('SQL_DEBUG',
                     'EMBEDDING_ENABLED',
                     'GENERATE_SQL_QUERY_LIMIT_ENABLED',
                     'PARSE_REASONING_BLOCK_ENABLED',
                     'PG_POOL_PRE_PING',
                     'TABLE_EMBEDDING_ENABLED',
                     'AGENTIC_SQL_RETRY_ENABLED',
                     'AGENTIC_GRADER_ENABLED',
                     'AGENTIC_MULTI_CANDIDATE_ENABLED',
                     'AGENTIC_DS_FALLBACK_ENABLED',
                     'AGENTIC_DECOMPOSE_ENABLED',
                     'AGENTIC_HYBRID_RANKING_ENABLED',
                     'AGENTIC_IDENTIFIER_CHECK_ENABLED',
                     'AGENTIC_SELF_CONSISTENCY_ENABLED',
                     'VALUE_LINKING_ENABLED',
                     'SKELETON_FEWSHOT_ENABLED',
                     'EXCEL_ISLAND_DETECTION_ENABLED',
                     'EXCEL_SPLIT_SIDE_BY_SIDE',
                     'EMBEDDING_SAMPLE_ENABLED',
                     'EXCEL_FTS_ENABLED',
                     'DS_SUMMARY_ENABLED',
                     mode='before')
    @classmethod
    def lowercase_bool(cls, v: Any) -> Any:
        """将字符串形式的布尔值转换为Python布尔值"""
        if isinstance(v, str):
            v_lower = v.lower().strip()
            if v_lower == 'true':
                return True
            elif v_lower == 'false':
                return False
        return v


settings = Settings()  # type: ignore
