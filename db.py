import sqlite3
from pathlib import Path
from typing import Any
from collections import defaultdict
from fastapi import HTTPException
from pydantic import BaseModel

DB_PATH = Path(__file__).parent / "data.db"


def _connect() -> sqlite3.Connection:
    """Единая точка открытия соединения — чтобы timeout/WAL не разъезжались
    по разным местам файла, если понадобится поменять настройки."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_cpu_vendors() -> list[str]:
    """Актуальный список вендоров CPU из БД + псевдо-значение "any"
    (означает "любой вендор", в БД не хранится)."""
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT vendor FROM cpus")
        vendors = [row[0] for row in cursor.fetchall() if row[0] not in (None, "")]
    return ["any", *vendors]


def get_works_main_values() -> list[str]:
    """Актуальный список типов ЧО (works_coefficient.service) из БД."""
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT service FROM works_coefficient")
        return [row[0] for row in cursor.fetchall() if row[0] not in (None, "")]


def get_capacity_disk_types() -> list[str]:
    """Актуальный список типов дисков хранения (capacity_disks.disk_type)."""
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT disk_type FROM capacity_disks")
        return [row[0] for row in cursor.fetchall() if row[0] not in (None, "")]


def get_vsan_types() -> list[str]:
    """Актуальный список типов vSAN (slack_space.vsan_type). Сейчас нигде
    не подключён — выбор ESA/OSA временно отключён на уровне API, см.
    main.py; функция готова к использованию, когда это снова понадобится."""
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT vsan_type FROM slack_space")
        return [row[0] for row in cursor.fetchall() if row[0] not in (None, "")]


# ---------------------------------------------------------------------------
# Справочные данные для калькулятора
#
# Единая точка чтения SQLite для requested_config(): собирает все справочные
# таблицы в один словарь. calculator.py ничего не знает о существовании БД.
# Вызывается заново на каждый запрос к /config — так изменения, сделанные
# через db_update.html, сразу видны без перезапуска сервера.
# ---------------------------------------------------------------------------


def load_db_data() -> dict[str, Any]:
    with _connect() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT name, value FROM parameters_float")
        parameters_float = {row[0]: float(row[1]) for row in cursor.fetchall()}

        cursor.execute("SELECT value FROM unbalanced_ram_module_counts")
        unbalanced_ram_module_counts = frozenset(int(row[0]) for row in cursor.fetchall())

        cursor.execute("SELECT vsan_type, slack_space FROM slack_space")
        slack_space = {row[0]: float(row[1]) for row in cursor.fetchall()}

        cursor.execute(
            "SELECT service, host_qty, coefficient FROM works_coefficient ORDER BY service, host_qty")
        works_coefficient: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for service, host_qty, coefficient in cursor.fetchall():
            works_coefficient[service].append((int(host_qty), float(coefficient)))
        works_coefficient = dict(works_coefficient)

        cursor.execute(
            "SELECT vendor, name, cores_quantity, cores_frequency, price, socket, ram_gen, seido "
            "FROM cpus WHERE is_active = 1"
        )
        cpus = tuple(
            {
                "vendor": row[0],
                "name": row[1],
                "cores_quantity": int(row[2]),
                "cores_frequency": int(row[3]),
                "price": int(row[4]),
                "socket": int(row[5]),
                "ram_gen": row[6],
                "seido": int(row[7]),
            }
            for row in cursor.fetchall()
        )

        cursor.execute(
            "SELECT name, vram, size, price, seido FROM videocards WHERE is_active = 1"
        )
        videocards = tuple(
            {
                "name": row[0],
                "vram": int(row[1]),
                "size": int(row[2]),
                "price": int(row[3]),
                "seido": int(row[4]),
            }
            for row in cursor.fetchall()
        )

        cursor.execute(
            "SELECT name, cpu_vendor, socket, max_ram, ram_gen, "
            "max_disks_qty, max_nvme_disks_qty, price, seido, gpu_support, gpu_size FROM servers WHERE is_active = 1"
        )
        servers = tuple(
            {
                "name": row[0],
                "cpu_vendor": row[1],
                "socket": int(row[2]),
                "max_ram": int(row[3]),
                "ram_gen": row[4],
                "max_disks_qty": int(row[5]),
                "max_nvme_disks_qty": int(row[6]),
                "price": int(row[7]),
                "seido": int(row[8]),
                "gpu_support": row[9],
                "gpu_size": row[10],
            }
            for row in cursor.fetchall()
        )

        cursor.execute("SELECT disk_type, capacity, price, seido FROM esxi_disks")
        esxi_disks = {
            row[0]: {"disk_type": row[1], "price": int(row[2]), "seido": int(row[3])} for row in cursor.fetchall()
        }

        cursor.execute("SELECT disk_type, capacity, price, seido FROM cache_disks")
        cache_disks = tuple(
            {"disk_type": row[0], "capacity": row[1], "price": int(row[2]), "seido": int(row[3])}
            for row in cursor.fetchall()
        )

        cursor.execute("SELECT name, price, seido FROM network_cards")
        network_cards = tuple(
            {"name": row[0], "price": int(row[1]), "seido": int(row[2])} for row in cursor.fetchall()
        )

        cursor.execute("SELECT spec, name, price, seido FROM hba_adapters")
        hba_adapters = {
            row[0]: {"name": row[1], "price": int(row[2]), "seido": int(row[3])} for row in cursor.fetchall()
        }

        cursor.execute("SELECT disk_type, capacity, price, seido FROM capacity_disks")
        capacity_disks: dict[str, dict[int, dict[str, Any]]] = {"ssd": {}, "nvme": {}}
        for row in cursor.fetchall():
            capacity_disks[row[0]][row[1]] = {"price": int(row[2]), "seido": int(row[3])}

        cursor.execute("SELECT ram_gen, ram_size, price, seido FROM rams")
        rams = tuple(
            {"ram_gen": row[0], "ram_size": int(row[1]), "price": int(row[2]), "seido": int(row[3])}
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
        "parameters_float": parameters_float,
        "unbalanced_ram_module_counts": unbalanced_ram_module_counts,
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
        "videocards": videocards,
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


class ColumnInfo(BaseModel):
    name: str
    type: str
    isPrimary: bool = False


class TableData(BaseModel):
    columns: list[ColumnInfo]
    rows: list[dict[str, Any]]


def get_all_table_names() -> list[str]:
    with _connect() as conn:
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


def get_table_data(table_name: str) -> TableData:
    known_tables = get_all_table_names()
    _validate_table_name(table_name, known_tables)

    with _connect() as conn:
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


def update_table_rows(table_name: str, rows: list[dict[str, Any]]) -> int:
    known_tables = get_all_table_names()
    _validate_table_name(table_name, known_tables)

    if not rows:
        return 0

    with _connect() as conn:
        cursor = conn.cursor()
        pragma = _get_table_pragma(cursor, table_name)
        pk_cols = [row[1] for row in pragma if row[5] > 0]
        use_rowid = len(pk_cols) != 1

        pk_field = "id" if use_rowid else pk_cols[0]  # ключ в JSON от фронта
        where_col = "rowid" if use_rowid else pk_cols[0]  # реальная колонка в SQL
        col_types = {row[1]: row[2] for row in pragma}
        # notnull-флаг из PRAGMA (row[3]) — на него опирается защита ниже:
        # очищенное на фронте поле NOT NULL-колонки не должно тихо стать
        # NULL в БД и сломать load_db_data() при следующем расчёте (там
        # такие колонки читаются без проверки на None, напрямую через
        # int(row[...])/float(row[...])).
        col_notnull = {row[1]: bool(row[3]) for row in pragma}

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

            # Проверяем NOT NULL до похода в SQLite — так пользователь
            # получит понятную ошибку с именем поля, а не общий
            # IntegrityError (или, если колонка формально допускает NULL,
            # молчаливую порчу справочника, которая проявится только при
            # следующем расчёте /config).
            for c in set_cols:
                if col_notnull.get(c) and row[c] in (None, ""):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Поле '{c}' не может быть пустым",
                    )

            set_clause = ", ".join(f'"{c}" = ?' for c in set_cols)
            values = [_coerce_value(row[c], col_types[c]) for c in set_cols]
            pk_type = "INTEGER" if use_rowid else col_types.get(pk_cols[0], "TEXT")
            values.append(_coerce_value(row[pk_field], pk_type))

            try:
                cursor.execute(
                    f'UPDATE "{table_name}" SET {set_clause} WHERE "{where_col}" = ?',
                    values,
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=400, detail=f"Ошибка обновления: {exc}")

            updated += 1 if cursor.rowcount > 0 else 0

        conn.commit()

    return updated


def insert_table_row(table_name: str, row: dict[str, Any]) -> dict[str, Any]:
    known_tables = get_all_table_names()
    _validate_table_name(table_name, known_tables)

    with _connect() as conn:
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
        # get_table_data(), чтобы фронтенд мог сразу подставить её на место
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


def delete_table_row(table_name: str, row_id: str) -> int:
    known_tables = get_all_table_names()
    _validate_table_name(table_name, known_tables)

    with _connect() as conn:
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

    return deleted