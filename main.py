import re
import sqlite3
from enum import Enum
from typing import Annotated, Any, cast
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from calculator import requested_config
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse

app = FastAPI()

BASE_DIR = Path(__file__).parent
INDEX_HTML = BASE_DIR / "static" / "main.html"
DB_HTML = BASE_DIR / "static" / "db_update.html"
DB_PATH = BASE_DIR / "data.db"


# ---------------------------------------------------------------------------
# Миграция схемы
#
# Добавляет колонку доступности (is_active) в servers и cpus, если её ещё
# нет. Идея: вместо удаления железа из БД при его уходе со склада — просто
# снимаем галочку в db_update.html. Калькулятор перестаёт предлагать эту
# позицию, но сама запись и история остаются в БД. Выполняется один раз
# при старте сервера; идемпотентна — повторный запуск ничего не ломает.
# ---------------------------------------------------------------------------


def _ensure_schema() -> None:
    tables_to_patch = ["servers", "cpus"]
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        existing_tables = {
            row[0]
            for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table in tables_to_patch:
            if table not in existing_tables:
                continue  # таблицы ещё нет — например, чистая тестовая БД
            columns = {
                row[1] for row in cursor.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            if "is_active" not in columns:
                cursor.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1'
                )
        conn.commit()


_ensure_schema()


# ---------------------------------------------------------------------------
# Enum'ы для входных query-параметров — строятся из БД при старте сервера
#
# CpuVendor/WorksMain/CapacityDiskType/VsanType раньше были захардкожены,
# хотя фактически дублируют значения, уже существующие в БД (vendor из
# cpus, service из works_coefficient, disk_type из capacity_disks, vsan_type
# из slack_space). Строим их динамически через функциональный API Enum().
#
# Решение (см. историю чата): строить один раз при старте сервера, а не на
# каждый запрос — это структурные данные (какие вообще бывают вендоры/типы),
# они меняются на порядки реже, чем цены и наличие. Если понадобится
# подхватывать новый вендор/тип без рестарта сервера — можно заменить эти
# Enum на обычный str + Depends-валидатор, но тогда пропадёт готовый
# выпадающий список в Swagger, и его придётся собирать вручную через
# кастомную OpenAPI-схему.
# ---------------------------------------------------------------------------


def _safe_enum_member_name(value: str) -> str:
    """Приводит значение из БД к валидному имени атрибута Python."""
    name = re.sub(r"\W", "_", str(value))
    if not name or name[0].isdigit():
        name = f"v_{name}"
    return name


def _build_str_enum(
    class_name: str,
    query: str,
    extra_members: dict[str, str] | None = None,
) -> type[Enum]:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(query)
        db_values = [row[0] for row in cursor.fetchall() if row[0] not in (None, "")]

    members: dict[str, str] = dict(extra_members or {})
    for value in db_values:
        members[_safe_enum_member_name(value)] = str(value)

    if not members:
        raise RuntimeError(
            f"Не удалось построить enum {class_name}: в БД нет данных ({query!r}). "
            f"Проверьте, что таблица заполнена."
        )

    return cast(type[Enum], Enum(class_name, members, type=str))


CpuVendor = _build_str_enum(
    "CpuVendor",
    "SELECT DISTINCT vendor FROM cpus",
    extra_members={"any": "any"},
)

WorksMain = _build_str_enum(
    "WorksMain",
    "SELECT DISTINCT service FROM works_coefficient",
)

CapacityDiskType = _build_str_enum(
    "CapacityDiskType",
    "SELECT DISTINCT disk_type FROM capacity_disks",
)

VsanType = _build_str_enum(
    "VsanType",
    "SELECT DISTINCT vsan_type FROM slack_space",
)


# ---------------------------------------------------------------------------
# Экспорт данных из БД
#
# Единая точка чтения SQLite: собирает все справочные таблицы в один
# словарь и отдаёт его в calculator.requested_config(). calculator.py
# больше не знает о существовании БД. Вызывается заново на каждый запрос
# к /config — так изменения, сделанные через db_update.html, сразу видны
# без перезапуска сервера.
# ---------------------------------------------------------------------------


def load_db_data() -> dict[str, Any]:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT param_name, param_value FROM parameters")
        parameters = {row[0]: float(row[1]) for row in cursor.fetchall()}

        cursor.execute("SELECT vsan_type, slack_space FROM slack_space")
        slack_space = {row[0]: float(row[1]) for row in cursor.fetchall()}

        cursor.execute(
            "SELECT service, hosts_8, hosts_16, hosts_24, hosts_32, hosts_64, "
            "hosts_96, hosts_128 FROM works_coefficient"
        )
        works_coefficient = {row[0]: tuple(row[1:]) for row in cursor.fetchall()}

        cursor.execute(
            "SELECT vendor, name, cores_quantity, cores_frequency, price, socket, ram_gen "
            "FROM cpus WHERE is_active = 1"
        )
        cpus = tuple(
            {
                "vendor": row[0],
                "name": row[1],
                "cores_quantity": row[2],
                "cores_frequency": row[3],
                "price": row[4],
                "socket": row[5],
                "ram_gen": row[6],
            }
            for row in cursor.fetchall()
        )

        cursor.execute(
            "SELECT name, cpu_vendor, socket, max_ram, ram_gen, "
            "max_disks_qty, max_nvme_disks_qty, price FROM servers WHERE is_active = 1"
        )
        servers = tuple(
            {
                "name": row[0],
                "cpu_vendor": row[1],
                "socket": row[2],
                "max_ram": row[3],
                "ram_gen": row[4],
                "max_disks_qty": int(row[5]),
                "max_nvme_disks_qty": int(row[6]),
                "price": row[7],
            }
            for row in cursor.fetchall()
        )

        cursor.execute("SELECT disk_type, capacity, price FROM esxi_disks")
        esxi_disks = {
            row[0]: {"disk_type": row[1], "price": row[2]} for row in cursor.fetchall()
        }

        cursor.execute("SELECT disk_type, capacity, price FROM cache_disks")
        cache_disks = tuple(
            {"disk_type": row[0], "capacity": row[1], "price": row[2]}
            for row in cursor.fetchall()
        )

        cursor.execute("SELECT name, price FROM network_cards")
        network_cards = tuple(
            {"name": row[0], "price": row[1]} for row in cursor.fetchall()
        )

        cursor.execute("SELECT spec, name, price FROM hba_adapters")
        hba_adapters = {
            row[0]: {"name": row[1], "price": row[2]} for row in cursor.fetchall()
        }

        cursor.execute("SELECT disk_type, capacity, price FROM capacity_disks")
        capacity_disks: dict[str, dict[int, dict[str, Any]]] = {"ssd": {}, "nvme": {}}
        for row in cursor.fetchall():
            capacity_disks[row[0]][row[1]] = {"price": row[2]}

        cursor.execute("SELECT ram_gen, ram_size, price FROM rams")
        rams = tuple(
            {"ram_gen": row[0], "ram_size": row[1], "price": row[2]}
            for row in cursor.fetchall()
        )

        cursor.execute(
            "SELECT raid_level, FTM, FTT, disk_usage_overhead FROM raid_config"
        )
        raid_config = {
            row[0]: {"FTM": row[1], "FTT": row[2], "disk_usage_overhead": row[3]}
            for row in cursor.fetchall()
        }

    return {
        "parameters": parameters,
        "slack_space": slack_space,
        "works_coefficient": works_coefficient,
        "cpus": cpus,
        "servers": servers,
        "esxi_disks": esxi_disks,
        "cache_disks": cache_disks,
        "network_cards": network_cards,
        "hba_adapters": hba_adapters,
        "capacity_disks": capacity_disks,
        "rams": rams,
        "raid_config": raid_config,
    }


# ---------------------------------------------------------------------------
# Универсальный CRUD для db_update.html
#
# db_update.html — это generic-редактор: он не знает заранее ни списка
# таблиц, ни их колонок, а просит backend рассказать об этом через
# PRAGMA table_info(). Это значит, что ниже нельзя параметризовать имя
# таблицы через "?" (SQLite не позволяет плейсхолдеры для идентификаторов),
# поэтому КАЖДЫЙ раз перед использованием table_name в f-строке SQL он
# сверяется с реальным списком таблиц из sqlite_master — это и есть защита
# от SQL-инъекции через путь запроса.
#
# Не у всех таблиц есть объявленный однозначный первичный ключ (например,
# `parameters(param_name, param_value)` его не имеет). db_update.html умеет
# редактировать строки только если ловит колонку "id"/isPrimary — поэтому
# для таблиц без единственного PK подставляется встроенный rowid SQLite
# под видом синтетической колонки "id" (read-only на фронте), и обновление
# строки идёт по нему.
# ---------------------------------------------------------------------------


def get_all_table_names() -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        return [row[0] for row in cursor.fetchall()]


def _validate_table_name(table_name: str, known_tables: list[str]) -> None:
    if table_name not in known_tables:
        raise HTTPException(status_code=404, detail=f"Таблица '{table_name}' не найдена")


def _get_table_pragma(cursor: sqlite3.Cursor, table_name: str) -> list[tuple]:
    # table_name уже провалидирован по белому списку вызывающей стороной
    cursor.execute(f'PRAGMA table_info("{table_name}")')
    return cursor.fetchall()  # (cid, name, type, notnull, dflt_value, pk)


def _coerce_value(value: Any, sql_type: str) -> Any:
    """Приводит строковое значение из HTML-инпута к типу колонки SQLite."""
    if value is None or value == "":
        return None
    sql_type = (sql_type or "").upper()
    try:
        if "BOOL" in sql_type:
            if isinstance(value, bool):
                return int(value)
            return 1 if str(value).strip().lower() in ("1", "true", "on", "yes") else 0
        if "INT" in sql_type:
            return int(value)
        if any(t in sql_type for t in ("REAL", "FLOA", "DOUB", "NUMERIC", "DECIMAL")):
            return float(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"Не удалось привести значение '{value}' к типу {sql_type}",
        )
    return str(value)


# ---- Модели ответа для /table/{table_name} ----

class ColumnInfo(BaseModel):
    name: str
    type: str
    isPrimary: bool = False


class TableData(BaseModel):
    columns: list[ColumnInfo]
    rows: list[dict[str, Any]]


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(INDEX_HTML)

@app.get("/db", include_in_schema=False)
def db_update():
    return FileResponse(DB_HTML)


@app.get(
    "/tables",
    response_model=list[str],
    summary="Список таблиц в БД",
    tags=["db_update"],
)
def get_tables() -> list[str]:
    return get_all_table_names()


@app.get(
    "/table/{table_name}",
    response_model=TableData,
    summary="Получить структуру и данные таблицы",
    tags=["db_update"],
)
def get_table(table_name: str) -> TableData:
    known_tables = get_all_table_names()
    _validate_table_name(table_name, known_tables)

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        pragma = _get_table_pragma(cursor, table_name)
        pk_cols = [row[1] for row in pragma if row[5] > 0]
        use_rowid = len(pk_cols) != 1

        if use_rowid:
            cursor.execute(f'SELECT rowid AS id, * FROM "{table_name}"')
        else:
            cursor.execute(f'SELECT * FROM "{table_name}"')

        col_names = [d[0] for d in cursor.description]
        rows = [dict(zip(col_names, row)) for row in cursor.fetchall()]

    if use_rowid:
        columns = [ColumnInfo(name="id", type="INTEGER", isPrimary=True)]
        columns += [
            ColumnInfo(name=row[1], type=row[2] or "TEXT", isPrimary=False)
            for row in pragma
        ]
    else:
        columns = [
            ColumnInfo(name=row[1], type=row[2] or "TEXT", isPrimary=(row[1] == pk_cols[0]))
            for row in pragma
        ]

    return TableData(columns=columns, rows=rows)


@app.post(
    "/table/{table_name}",
    summary="Обновить строки таблицы",
    tags=["db_update"],
)
def update_table(table_name: str, rows: list[dict[str, Any]]) -> dict[str, int]:
    known_tables = get_all_table_names()
    _validate_table_name(table_name, known_tables)

    if not rows:
        return {"updated": 0}

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        pragma = _get_table_pragma(cursor, table_name)
        pk_cols = [row[1] for row in pragma if row[5] > 0]
        use_rowid = len(pk_cols) != 1

        pk_field = "id" if use_rowid else pk_cols[0]  # ключ в JSON от фронта
        where_col = "rowid" if use_rowid else pk_cols[0]  # реальная колонка в SQL
        col_types = {row[1]: row[2] for row in pragma}

        updated = 0
        for row in rows:
            if pk_field not in row:
                raise HTTPException(
                    status_code=400,
                    detail=f"В строке отсутствует ключ '{pk_field}'",
                )

            set_cols = [c for c in row.keys() if c != pk_field and c in col_types]
            if not set_cols:
                continue

            set_clause = ", ".join(f'"{c}" = ?' for c in set_cols)
            values = [_coerce_value(row[c], col_types[c]) for c in set_cols]
            pk_type = "INTEGER" if use_rowid else col_types.get(pk_cols[0], "TEXT")
            values.append(_coerce_value(row[pk_field], pk_type))

            cursor.execute(
                f'UPDATE "{table_name}" SET {set_clause} WHERE "{where_col}" = ?',
                values,
            )
            updated += 1 if cursor.rowcount > 0 else 0

        conn.commit()

    return {"updated": updated}


@app.post(
    "/table/{table_name}/rows",
    summary="Добавить новую строку в таблицу",
    tags=["db_update"],
)
def create_table_row(table_name: str, row: dict[str, Any]) -> dict[str, Any]:
    known_tables = get_all_table_names()
    _validate_table_name(table_name, known_tables)

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        pragma = _get_table_pragma(cursor, table_name)
        pk_cols = [row_[1] for row_ in pragma if row_[5] > 0]
        use_rowid = len(pk_cols) != 1
        col_types = {row_[1]: row_[2] for row_ in pragma}

        # PK-колонку не просим у клиента только тогда, когда она является
        # rowid-псевдонимом (обычная rowid-таблица без объявленного PK, либо
        # настоящий "INTEGER PRIMARY KEY") — в этих случаях SQLite назначит
        # следующее значение сам, если не указывать колонку в INSERT.
        # Для PK другого типа (например TEXT) SQLite НЕ требует NOT NULL
        # автоматически — вставка без него тихо создала бы строку с NULL
        # в первичном ключе, поэтому такой PK обязателен во входных данных.
        if use_rowid:
            pk_field_to_exclude = "id"
        else:
            pk_col = pk_cols[0]
            is_integer_pk = "INT" in (col_types.get(pk_col) or "").upper()
            if is_integer_pk:
                pk_field_to_exclude = pk_col
            else:
                if row.get(pk_col) in (None, ""):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Укажите значение первичного ключа '{pk_col}'",
                    )
                pk_field_to_exclude = None

        insert_cols = [
            c for c in row.keys()
            if c != pk_field_to_exclude and c in col_types
        ]
        if not insert_cols:
            raise HTTPException(status_code=400, detail="Нет данных для вставки строки")

        col_list = ", ".join(f'"{c}"' for c in insert_cols)
        placeholders = ", ".join("?" for _ in insert_cols)
        values = [_coerce_value(row[c], col_types[c]) for c in insert_cols]

        try:
            cursor.execute(
                f'INSERT INTO "{table_name}" ({col_list}) VALUES ({placeholders})',
                values,
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=400, detail=f"Ошибка вставки: {exc}")

        new_rowid = cursor.lastrowid
        conn.commit()

        # Отдаём созданную строку обратно в том же виде, в каком её увидит
        # GET /table/{name}, чтобы фронтенд мог сразу подставить её на место
        # черновика без повторной перезагрузки всей таблицы.
        if use_rowid:
            cursor.execute(f'SELECT rowid AS id, * FROM "{table_name}" WHERE rowid = ?', (new_rowid,))
        else:
            cursor.execute(f'SELECT * FROM "{table_name}" WHERE rowid = ?', (new_rowid,))
        col_names = [d[0] for d in cursor.description]
        fetched = cursor.fetchone()
        if fetched is None:
            raise HTTPException(status_code=500, detail="Строка создана, но не удалось её прочитать")
        created_row = dict(zip(col_names, fetched))

    return created_row


@app.delete(
    "/table/{table_name}/rows/{row_id}",
    summary="Удалить строку из таблицы",
    tags=["db_update"],
)
def delete_table_row(table_name: str, row_id: str) -> dict[str, int]:
    known_tables = get_all_table_names()
    _validate_table_name(table_name, known_tables)

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        pragma = _get_table_pragma(cursor, table_name)
        pk_cols = [row_[1] for row_ in pragma if row_[5] > 0]
        use_rowid = len(pk_cols) != 1
        where_col = "rowid" if use_rowid else pk_cols[0]
        pk_type = "INTEGER" if use_rowid else next(
            (row_[2] for row_ in pragma if row_[1] == pk_cols[0]), "TEXT"
        )

        value = _coerce_value(row_id, pk_type)
        cursor.execute(f'DELETE FROM "{table_name}" WHERE "{where_col}" = ?', (value,))
        deleted = cursor.rowcount
        conn.commit()

    return {"deleted": deleted}


# ---- Enum'ы для входных query-параметров теперь строятся из БД ----
# (см. блок сразу после _ensure_schema() в начале файла)


# ---- Модель входных query-параметров ----

class RequestedConfigParams(BaseModel):
    vcpu: int = Field(..., gt=0, description="Количество vCPU")
    vram: int = Field(..., gt=0, description="Объём RAM, МБ")
    vssd: int = Field(..., gt=0, description="Объём SSD, ГБ/МБ")
    cpu_min_frequency: int = Field(..., gt=0, description="Минимальная частота CPU, МГц")
    cpu_overcommit: int = Field(..., gt=0, description="Overcommit по CPU")
    # CpuVendor/WorksMain/CapacityDiskType/VsanType строятся динамически из
    # БД (см. _build_str_enum() выше), поэтому Pylance не может статически
    # проверить их как типы аннотаций — подавляем именно эту проверку,
    # pydantic на рантайме всё равно валидирует значения как обычный Enum.
    cpu_vendor: CpuVendor = CpuVendor.any  # pyright: ignore[reportInvalidTypeForm, reportAttributeAccessIssue]
    works_main: WorksMain  # pyright: ignore[reportInvalidTypeForm]
    capacity_disk_type: CapacityDiskType  # pyright: ignore[reportInvalidTypeForm]
    vsan_type: VsanType  # pyright: ignore[reportInvalidTypeForm]

    model_config = ConfigDict(extra="forbid")


# ---- Модель одного варианта конфигурации в ответе ----

class ConfigOption(BaseModel):
    hosts_by_cpu: int = Field(..., alias="Need hosts by CPU")
    cpu_redundancy: str = Field(..., alias="CPU redundancy")
    all_flash_vsan: str = Field(..., alias="AllFlash vSAN")
    failures_to_tolerate: int = Field(..., alias="Failures to Tolerate")
    cpu_overcommit: str = Field(..., alias="CPU overcommit")
    cpu: str = Field(..., alias="CPU")
    server: str = Field(..., alias="Server")
    ram: str = Field(..., alias="RAM")
    esxi_disk: str = Field(..., alias="Esxi disk")
    cache_disk: str = Field(..., alias="Cache disk")
    capacity_disk: str = Field(..., alias="Capacity disk")
    network_card: str = Field(..., alias="Network card")
    hba_adapter: str = Field(..., alias="HBA adapter")
    admin_main_works: str = Field(..., alias="Admin main works")
    vcpu_available: str = Field(..., alias="vCPU available")
    vram_available: str = Field(..., alias="vRAM available")
    vssd_available: str = Field(..., alias="vSSD available")
    total_price_rub: str = Field(..., alias="Total price, Rub")

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",  # теперь набор полей фиксирован — строгая валидация
        json_schema_extra={
            "example": {
                "Need hosts by CPU": 7,
                "CPU redundancy": "n+1",
                "AllFlash vSAN": "RAID-6",
                "Failures to Tolerate": 2,
                "CPU overcommit": "1",
                "CPU": "EPYC 9474F (48x3.6 GHz) - 2 шт",
                "Server": "ASUS RS720A-E12-RS12 - 1 шт",
                "RAM": "128Gb DDR5 4 шт",
                "Esxi disk": "500Gb m.2 nvme - 1 шт",
                "Cache disk": "1.6-3.2Gb nvme - 2 шт",
                "Capacity disk": "1920 nvme - 4 шт",
                "Network card": "25GB DUAL MCX512A - 1 шт",
                "HBA adapter": "-",
                "Admin main works": "vsphere",
                "vCPU available": "461",
                "vRAM available": "2458",
                "vSSD available": "24576",
                "Total price, Rub": "1640841 руб.",
            }
        },
    )



# ---- Эндпоинт ----

@app.get(
    "/config",
    response_model=list[ConfigOption],
    summary="Получить варианты конфигурации по заданным параметрам",
    tags=["config"],
)
def get_requested_config(
    params: Annotated[RequestedConfigParams, Query()],
) -> list[ConfigOption]:
    db_data = load_db_data()
    raw_result = requested_config(
        db_data=db_data,
        vcpu=params.vcpu,
        vram=params.vram,
        vssd=params.vssd,
        cpu_min_frequency=params.cpu_min_frequency,
        cpu_overcommit=params.cpu_overcommit,
        cpu_vendor=params.cpu_vendor.value,
        works_main=params.works_main.value,
        capacity_disk_type=params.capacity_disk_type.value,
        vsan_type=params.vsan_type.value,
    )

    return [ConfigOption.model_validate(item) for item in raw_result]