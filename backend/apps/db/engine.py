# Author: Junjun
# Date: 2025/5/19
import urllib.parse

from sqlalchemy import create_engine

from apps.datasource.models.datasource import DatasourceConf
from common.core.config import settings


def get_engine_config():
    return DatasourceConf(username=settings.POSTGRES_USER, password=settings.POSTGRES_PASSWORD,
                          host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT, database=settings.POSTGRES_DB,
                          dbSchema="public", timeout=30) # read engine config


def get_engine_uri(conf: DatasourceConf):
    return f"postgresql+psycopg2://{urllib.parse.quote(conf.username)}:{urllib.parse.quote(conf.password)}@{conf.host}:{conf.port}/{urllib.parse.quote(conf.database)}"


def get_engine_conn():
    conf = get_engine_config()
    db_url = get_engine_uri(conf)
    engine = create_engine(db_url,
                           connect_args={"options": f"-c search_path={conf.dbSchema}", "connect_timeout": conf.timeout},
                           pool_timeout=conf.timeout)
    return engine
