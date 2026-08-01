import secrets
import urllib.parse
from typing import Annotated, Any, Literal

from pydantic import (
    AnyUrl,
    BeforeValidator,
    PostgresDsn,
    computed_field,
    field_validator,
    model_validator
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
    # Deployment posture. Only "production" turns the dev-origin CORS check
    # into an error; everything else stays developer-friendly (AUDIT H-02).
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"
    FRONTEND_HOST: str = "http://localhost:5173"

    BACKEND_CORS_ORIGINS: Annotated[
        list[AnyUrl] | str, BeforeValidator(parse_cors)
    ] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def all_cors_origins(self) -> list[str]:
        # De-duplicated, order preserved. FRONTEND_HOST is appended
        # unconditionally, so listing it in BACKEND_CORS_ORIGINS too used to
        # emit it twice (AUDIT H-02); `validate_settings` refuses a localhost
        # origin when ENVIRONMENT=production.
        origins = [str(origin).rstrip("/") for origin in self.BACKEND_CORS_ORIGINS]
        if self.FRONTEND_HOST:
            origins.append(self.FRONTEND_HOST.rstrip("/"))
        return list(dict.fromkeys(origins))

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
    # Module-global thread pools. These were two hardcoded 200-worker pools =
    # 400 threads on a --workers 1 uvicorn, each able to hold a DB session, a
    # NullPool datasource connection and an LLM stream, against a PG_POOL_SIZE
    # of 20 (AUDIT D-14). Sized to leave headroom over the connection pool
    # rather than to an arbitrary round number; raise if you also raise
    # PG_POOL_SIZE.
    LLM_EXECUTOR_MAX_WORKERS: int = 64
    EMBEDDING_EXECUTOR_MAX_WORKERS: int = 32
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
    # Cross-model voting: route the FIRST extra candidate to a different model on
    # the same endpoint/credentials (e.g. 'qwen2.5-coder:7b'). Two models fail on
    # different questions, so a vote across them covers strictly more than
    # resampling the primary — and the tie-break still favours the primary, so
    # the alternate can only win by corroborating one of its answers. Empty
    # keeps every candidate on the primary model.
    AGENTIC_SELF_CONSISTENCY_MODEL: str = ''

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
    TABLE_SAMPLE_CHAR_BUDGET: int = 1200
    TABLE_SAMPLE_DISTINCT_PER_COL: int = 15
    TABLE_SAMPLE_VALUE_MAXLEN: int = 60
    # Ceiling on the COMBINED sample block across all tables. The per-table budget
    # above is applied per table, so with TABLE_EMBEDDING_COUNT tables it multiplies:
    # at the old 3000/table x 10 tables the sample block alone could reach 30k chars
    # and 22% of LLM calls hit the model's 32k context ceiling, truncating the prompt
    # and producing empty answers (NO-SQL 4 -> 13 on BIRD, 12 of them on the largest
    # schema). This cap is the backstop that makes the total bounded regardless of
    # table count. 0 disables it.
    TABLE_SAMPLE_TOTAL_CHAR_BUDGET: int = 9000

    # Statement (not connection) timeout for generated SQL, in seconds. 0 disables.
    # connect_timeout only bounds opening the connection: without this one
    # pathological generated query runs forever and pins a connection and a worker.
    DS_STATEMENT_TIMEOUT: int = 120

    # Hard cap on tokens the model may generate per call. 0 disables (previous
    # behaviour). Without it a model that falls into a repetition loop generates
    # until it exhausts the whole context window: measured on BIRD, one question
    # burned ~9.5k tokens over 45 MINUTES emitting the same clause forever, and
    # nothing could stop it. Greedy decoding (temperature 0) makes this more
    # likely, not less, because there is no sampling noise to break the cycle.
    # 1500 is far above any real SQL answer and turns that 45min into ~4min.
    LLM_MAX_OUTPUT_TOKENS: int = 1500
    # "list all X" completeness: lift an over-small model LIMIT up to this cap.
    AGENTIC_FULL_RESULT_LIMIT: int = 1000
    # Deterministic `ORDER BY x DESC` -> `... DESC NULLS LAST` rewrite before
    # execution. PostgreSQL and Oracle sort NULL as larger than any value, so
    # `ORDER BY score DESC LIMIT 1` returns a NULL row instead of the maximum.
    # The SQL prompt already asks for NULLS LAST and the model ignored it on 17
    # of 72 BIRD-150 failures; rewriting is free and was measured at +5 questions
    # (+3.3pp EX) with 0 regressions. Costs no LLM call.
    AGENTIC_NULLS_LAST_ENABLED: bool = True
    # Only dialects that BOTH sort NULLs first on DESC and accept NULLS LAST.
    # MySQL/SQL Server/SQLite/ClickHouse/Hive already sort NULLs last on DESC and
    # mostly reject the syntax, so they are deliberately absent.
    AGENTIC_NULLS_LAST_DIALECTS: str = 'pg,excel,kingbase,redshift,oracle,dm'
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
    # Terms that make a nearby small number an EXPLICIT row request ("top 10",
    # "show 5 records", "상위 5개"), which suppresses the completeness LIMIT lift.
    # Hard cap on rows serialised into the chat record's Text column. This is a
    # STORAGE guard and is deliberately independent of enable_sql_row_limit,
    # which governs whether generated SQL carries a LIMIT (AUDIT D-16).
    AGENTIC_PERSISTED_ROW_CAP: int = 1000
    # APEX pruning. apex_helpers assumed 4 chars/token; llm.py measured 2.9 on
    # this workload and its own comment notes 4 "understated the true size by
    # ~40%", so batches ran ~38% over budget (AUDIT H-08). The caps below were
    # literals in the hottest, most expensive stage of the pipeline and could
    # not be tuned without a rebuild (AUDIT H-09).
    # Model-facing error text was truncated by a bare [:1500] in four places
    # (AUDIT H-11).
    LLM_ERROR_TEXT_MAX_CHARS: int = 1500
    # Asia/Shanghai is baked into the image, so every datetime.now() -- including
    # the prompt's {current_time} slot -- is CST regardless of tenant (H-17).
    # Empty keeps the container's local time, i.e. today's behaviour exactly.
    PROMPT_TIMEZONE: str = ""
    APEX_CHARS_PER_TOKEN: float = 2.9
    APEX_MAX_PROBE_TABLES: int = 8
    APEX_MAX_PROBES: int = 8
    APEX_MIN_COLUMNS_TO_PRUNE: int = 12
    APEX_MAX_WORKERS: int = 2
    # A single call takes 30-120 s on the reference hardware, so a 60 s cap
    # paid for the alternate candidate and then discarded it (AUDIT H-10).
    LLM_ALT_CANDIDATE_TIMEOUT: int = 180
    AGENTIC_ROW_COUNT_KEYWORDS: str = (
        'top,first,last,limit,bottom,head,'
        'record,records,row,rows,result,results,item,items,entry,entries,'
        '前,最前,头,条,상위,하위,개,건')
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

    # --- Join graph in the schema block ---------------------------------------
    # The schema sent to the model lists columns and types but nothing about how
    # tables relate, so joins are guessed. Measured on BIRD Mini-Dev that is the
    # dominant error axis (1-table questions score ~2x 3-table ones; the two
    # biggest buckets are extra-join and missing-join). Declared PK/FK are read
    # from information_schema; spreadsheets have none, so edges are inferred from
    # column naming plus real value overlap and marked as inferred in the prompt.
    SCHEMA_RELATIONS_ENABLED: bool = True
    SCHEMA_RELATIONS_INFER: bool = True       # inference for Excel/CSV (no FKs)
    SCHEMA_RELATIONS_CACHE_TTL: int = 3600    # seconds; schemas change rarely
    SCHEMA_RELATIONS_SAMPLE: int = 500        # distinct values probed per column
    SCHEMA_RELATIONS_MIN_OVERLAP: float = 0.5  # |A∩B| / min(|A|,|B|) to accept
    # Workbooks usually join a detail sheet to a summary sheet on a business
    # label ("Category", "Region") with no key-shaped name anywhere, so those
    # are admitted too -- but only on near-total value agreement.
    SCHEMA_RELATIONS_MIN_OVERLAP_UNNAMED: float = 0.9
    SCHEMA_RELATIONS_MIN_UNIQUENESS: float = 0.9  # parent side must look like a key
    SCHEMA_RELATIONS_MAX_FANOUT: int = 6      # a column in >N tables is a label
    SCHEMA_RELATIONS_MAX_PAIRS: int = 200     # probe budget per datasource

    ORACLE_CLIENT_PATH: str = '/opt/sqlbot/db_client/oracle_instant_client'

    @model_validator(mode="after")
    def _propagate_embedding_defaults(self) -> "Settings":
        """Move the children when the parent knob moves.

        `EMBEDDING_TERMINOLOGY_SIMILARITY: float = EMBEDDING_DEFAULT_SIMILARITY`
        binds the class-body VALUE at definition time, so overriding
        EMBEDDING_DEFAULT_SIMILARITY in the environment moved nothing and the
        operator had to find and set all three (AUDIT H-06). `model_fields_set`
        holds only the fields explicitly supplied, so a deliberately-set child
        still wins.
        """
        for parent, children in (
            ("EMBEDDING_DEFAULT_SIMILARITY",
             ("EMBEDDING_TERMINOLOGY_SIMILARITY", "EMBEDDING_DATA_TRAINING_SIMILARITY")),
            ("EMBEDDING_DEFAULT_TOP_COUNT",
             ("EMBEDDING_TERMINOLOGY_TOP_COUNT", "EMBEDDING_DATA_TRAINING_TOP_COUNT")),
        ):
            if parent not in self.model_fields_set:
                continue
            for child in children:
                if child not in self.model_fields_set:
                    object.__setattr__(self, child, getattr(self, parent))
        return self

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
                     'AGENTIC_NULLS_LAST_ENABLED',
                     'AGENTIC_SELF_CONSISTENCY_ENABLED',
                     'VALUE_LINKING_ENABLED',
                     'SKELETON_FEWSHOT_ENABLED',
                     'EXCEL_ISLAND_DETECTION_ENABLED',
                     'EXCEL_SPLIT_SIDE_BY_SIDE',
                     'EMBEDDING_SAMPLE_ENABLED',
                     'EXCEL_FTS_ENABLED',
                     'DS_SUMMARY_ENABLED',
                     'SCHEMA_RELATIONS_ENABLED',
                     'SCHEMA_RELATIONS_INFER',
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


class ConfigurationError(RuntimeError):
    """A setting combination that cannot work. Raised at startup, never later.

    Every case here previously produced a system that *ran* and was silently
    wrong -- an unreachable cache, an unresolvable image URL, a dev origin
    accepted in production. Failing at boot is the whole point (AUDIT H-01/02/03).
    """


def collect_config_warnings(s: "Settings | None" = None) -> list[str]:
    """Non-fatal misconfigurations worth shouting about at startup."""
    s = s or settings
    warnings: list[str] = []

    if "YOUR_SERVE_IP" in s.SERVER_IMAGE_HOST or "MCP_PORT" in s.SERVER_IMAGE_HOST:
        warnings.append(
            f"SERVER_IMAGE_HOST is still the placeholder {s.SERVER_IMAGE_HOST!r}: "
            f"every MCP/API chart reply will return an unresolvable image URL. "
            f"Set it to this server's externally reachable base URL.")

    if s.ENVIRONMENT != "production" and s.POSTGRES_PASSWORD == "Password123@pg":
        warnings.append(
            "POSTGRES_PASSWORD is the shipped default; it is also baked into the "
            "image as an ENV. Override it before exposing this deployment.")

    return warnings


def validate_settings(s: "Settings | None" = None) -> None:
    """Fail fast on setting combinations that cannot work.

    Called from main.py at import time so a misconfigured container dies at boot
    with the reason, instead of serving traffic and misbehaving quietly.
    """
    s = s or settings
    errors: list[str] = []

    # H-03: an empty URL falls back to redis://localhost:6379/0, which inside a
    # container is nothing at all -- the cache silently does not work.
    if s.CACHE_TYPE == "redis" and not s.CACHE_REDIS_URL:
        errors.append("CACHE_TYPE=redis requires CACHE_REDIS_URL to be set.")

    # H-02: FRONTEND_HOST defaults to the Vite dev origin and is appended to
    # all_cors_origins unconditionally.
    if s.ENVIRONMENT == "production":
        dev_origins = [o for o in s.all_cors_origins
                       if "localhost" in o or "127.0.0.1" in o]
        if dev_origins:
            errors.append(
                f"ENVIRONMENT=production permits development CORS origins "
                f"{dev_origins}. Set FRONTEND_HOST (and BACKEND_CORS_ORIGINS) to "
                f"the real front-end origin.")
        if s.SECRET_KEY == "changethis":
            errors.append("SECRET_KEY is still the placeholder value.")

    if s.ROW_RAG_EMBED_DIM <= 0:
        errors.append(f"ROW_RAG_EMBED_DIM must be positive, got {s.ROW_RAG_EMBED_DIM}.")

    if errors:
        raise ConfigurationError(
            "Invalid configuration:\n  - " + "\n  - ".join(errors))
