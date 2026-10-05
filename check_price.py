"""
Скрипт обновления цен (price) в БД калькулятора на основе внешнего
API-справочника комплектующих.

Логика:
  1. Открывает SQLite БД.
  2. Сам находит все таблицы, у которых есть колонка seido (и отдельно —
     у которых есть сразу seido и seido2, как у servers) — списки таблиц
     не хардкодятся, а вычисляются по факту через PRAGMA table_info.
  3. Забирает большой справочник по API_URL (список словарей "железка":
     id, price.rub, ...).
  4. Для каждой таблицы с одной колонкой seido находит по её значению
     нужную позицию в справочнике, пересчитывает цену по формуле и
     записывает обратно в колонку price.
  5. Для каждой таблицы с колонками seido + seido2 (сейчас это только
     servers, но будет работать и для любой новой такой таблицы) находит
     обе позиции, считает цену для каждой, складывает и записывает в price.
  6. Если у таблицы есть колонка is_active — выставляет её в 1, когда цена
     найдена (для dual-таблиц — когда найдены обе позиции), и в 0, когда
     цена не найдена; при is_active=0 старое значение price не трогается.

Формула пересчёта:
    price = int(rms_price * payback_period / coefficient_dedicated / currency)
где payback_period, coefficient_dedicated, currency берутся из таблицы
parameters_float (колонки id, name, value — по одной строке на параметр).

Запуск вручную:
    python3 update_prices.py
"""

import json
import logging
import sqlite3
import sys
import requests
from pathlib import Path

# ----------------------------------------------------------------------------
# НАСТРОЙКИ — поправьте под свой проект перед первым запуском
# ----------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "data.db"
API_URL = "https://api.selectel.ru/servers/v2/pub/calculator/items"
LOG_PATH =  Path(__file__).parent / "update_prices.log"
REQUEST_TIMEOUT = 30                         

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
_logger = logging.getLogger("update_prices")


def log(msg: str) -> None:
    _logger.info(msg)


def discover_seido_tables(conn: sqlite3.Connection):
    """
    Сама находит нужные таблицы вместо хардкода списка:
    - single: таблицы, где есть seido, но нет seido2 (комплектующие)
    - dual:   таблицы, где есть и seido, и seido2 (сейчас только servers)
    Для каждой таблицы также запоминает, есть ли у неё колонка is_active —
    если есть, она будет проставляться в True/False по факту нахождения цены.
    Каждый элемент результата — пара (имя_таблицы, есть_is_active).
    Так при появлении новой таблицы с колонкой seido (и/или is_active) её
    не нужно вручную дописывать в код.
    """
    tables = [
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    ]

    single, dual = [], []
    for table in tables:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "seido" not in cols:
            continue
        has_is_active = "is_active" in cols
        if "seido2" in cols:
            dual.append((table, has_is_active))
        else:
            single.append((table, has_is_active))

    log(f"Таблицы с одной seido: {single}")
    log(f"Таблицы с seido+seido2: {dual}")
    return single, dual


def fetch_catalog() -> dict:
    """Запрос к API, возвращает словарь {id: item}."""
    try:
        resp = requests.get(API_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log(f"Ошибка запроса к API: {exc}")
        sys.exit(1)

    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        log(f"Ответ не является JSON: {exc}")
        sys.exit(1)

    items_by_id = {}
    for item in data:
        items_by_id[item["id"]] = item

    log(f"Справочник получен: {len(items_by_id)} позиций")
    return items_by_id


def load_parameters(conn: sqlite3.Connection) -> dict:
    """parameters_float: колонки id, name, value — по одной строке на параметр."""
    params = dict(conn.execute("SELECT name, value FROM parameters_float"))

    for key in ("payback_period", "coefficient_dedicated", "currency"):
        if key not in params:
            log(f"В parameters_float нет параметра '{key}' — проверьте БД")
            sys.exit(1)

    return params


def calc_price(rms_price: float, params: dict) -> int:
    """price = int(rms_price * payback_period / coefficient_dedicated / currency)"""
    return int(
        rms_price
        * params["payback_period"]
        / params["coefficient_dedicated"]
        / params["currency"]
    )


def get_item_rms_price(items_by_id: dict, seido):
    """Достаёт rms_price (price.rub) из справочника по номеру seido."""
    if seido is None:
        return None
    item = items_by_id.get(seido)
    if item is None:
        return None
    return item.get("price", {}).get("rub", 0)


def update_single_seido_tables(conn: sqlite3.Connection, tables: list, items_by_id: dict, params: dict) -> None:
    for table, has_is_active in tables:
        rows = conn.execute(f"SELECT rowid, seido FROM {table}").fetchall()

        updated, deactivated, missing = 0, 0, []
        for rowid, seido in rows:
            rms_price = get_item_rms_price(items_by_id, seido)
            if rms_price is None:
                missing.append(seido)
                if has_is_active:
                    conn.execute(f"UPDATE {table} SET is_active = 0 WHERE rowid = ?", (rowid,))
                    deactivated += 1
                continue
            new_price = calc_price(rms_price, params)
            if has_is_active:
                conn.execute(
                    f"UPDATE {table} SET price = ?, is_active = 1 WHERE rowid = ?",
                    (new_price, rowid),
                )
            else:
                conn.execute(f"UPDATE {table} SET price = ? WHERE rowid = ?", (new_price, rowid))
            updated += 1

        log(f"{table}: обновлено {updated} из {len(rows)}"
            + (f", is_active=0 у {deactivated}" if has_is_active else "")
            + (f", не найдены в справочнике seido={missing}" if missing else ""))


def update_dual_seido_tables(conn: sqlite3.Connection, tables: list, items_by_id: dict, params: dict) -> None:
    for table, has_is_active in tables:
        rows = conn.execute(f"SELECT rowid, seido, seido2 FROM {table}").fetchall()

        updated, deactivated, missing = 0, 0, []
        for rowid, seido, seido2 in rows:
            rms_price_1 = get_item_rms_price(items_by_id, seido)
            rms_price_2 = get_item_rms_price(items_by_id, seido2)

            if rms_price_1 is None:
                missing.append(seido)
            if rms_price_2 is None:
                missing.append(seido2)
            if rms_price_1 is None or rms_price_2 is None:
                if has_is_active:
                    conn.execute(f"UPDATE {table} SET is_active = 0 WHERE rowid = ?", (rowid,))
                    deactivated += 1
                continue

            total_price = calc_price(rms_price_1, params) + calc_price(rms_price_2, params)
            if has_is_active:
                conn.execute(
                    f"UPDATE {table} SET price = ?, is_active = 1 WHERE rowid = ?",
                    (total_price, rowid),
                )
            else:
                conn.execute(f"UPDATE {table} SET price = ? WHERE rowid = ?", (total_price, rowid))
            updated += 1

        log(f"{table}: обновлено {updated} из {len(rows)}"
            + (f", is_active=0 у {deactivated}" if has_is_active else "")
            + (f", не найдены в справочнике seido={missing}" if missing else ""))


def main() -> None:
    log("Старт обновления цен")

    items_by_id = fetch_catalog()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        single_tables, dual_tables = discover_seido_tables(conn)
        params = load_parameters(conn)
        update_single_seido_tables(conn, single_tables, items_by_id, params)
        update_dual_seido_tables(conn, dual_tables, items_by_id, params)
        conn.commit()
        log("Готово, изменения сохранены")
    except Exception:
        conn.rollback()
        log("Ошибка во время обновления, изменения откачены")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()