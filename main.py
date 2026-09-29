import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

import db
from calculator import requested_config

app = FastAPI()

BASE_DIR = Path(__file__).parent
INDEX_HTML = BASE_DIR / "static" / "main.html"
DB_HTML = BASE_DIR / "static" / "db_update.html"


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(INDEX_HTML)


@app.get("/db", include_in_schema=False)
def db_update():
    return FileResponse(DB_HTML)


# ---------------------------------------------------------------------------
# Универсальный CRUD для db_update.html — сами SQL-запросы и защита от
# инъекций живут в db.py, здесь только HTTP-обёртка над ними.
# ---------------------------------------------------------------------------


@app.get(
    "/tables",
    response_model=list[str],
    summary="Список таблиц в БД",
    tags=["db_update"],
)
def get_tables() -> list[str]:
    return db.get_all_table_names()


@app.get(
    "/table/{table_name}",
    response_model=db.TableData,
    summary="Получить структуру и данные таблицы",
    tags=["db_update"],
)
def get_table(table_name: str) -> db.TableData:
    return db.get_table_data(table_name)


# ---------------------------------------------------------------------------
# Единый источник значений для enum-полей формы main.html (Тип ЧО, Тип
# дисков, Вендор CPU). Сами значения уже строятся динамически из БД в
# db.py (CpuVendor/WorksMain/CapacityDiskType, см. _build_str_enum) — этот
# эндпоинт просто отдаёт их на фронт, чтобы там не приходилось дублировать
# список вручную и править в нескольких местах при добавлении нового
# вендора/типа ЧО/типа диска в БД.
# ---------------------------------------------------------------------------


class FieldOptions(BaseModel):
    works_main: list[str]
    cpu_vendor: list[str]
    capacity_disk_type: list[str]


@app.get(
    "/field-options",
    response_model=FieldOptions,
    summary="Допустимые значения enum-полей формы (единый источник для фронтенда)",
    tags=["config"],
)
def get_field_options() -> FieldOptions:
    return FieldOptions(
        works_main=db.get_works_main_values(),
        cpu_vendor=db.get_cpu_vendors(),
        capacity_disk_type=db.get_capacity_disk_types(),
    )


@app.post(
    "/table/{table_name}",
    summary="Обновить строки таблицы",
    tags=["db_update"],
)
def update_table(table_name: str, rows: list[dict[str, Any]]) -> dict[str, int]:
    return {"updated": db.update_table_rows(table_name, rows)}


@app.post(
    "/table/{table_name}/rows",
    summary="Добавить новую строку в таблицу",
    tags=["db_update"],
)
def create_table_row(table_name: str, row: dict[str, Any]) -> dict[str, Any]:
    return db.insert_table_row(table_name, row)


@app.delete(
    "/table/{table_name}/rows/{row_id}",
    summary="Удалить строку из таблицы",
    tags=["db_update"],
)
def delete_table_row(table_name: str, row_id: str) -> dict[str, int]:
    return {"deleted": db.delete_table_row(table_name, row_id)}


# ---- Модель профиля рабочего стола VDI ----
#
# Приходит от фронтенда (см. VDI_PROFILES в main.html) только когда
# works_main == "vdi", как JSON-строка в query-параметре vdi_profiles.
# Фронтенд уже сам сворачивает профили в суммарные vcpu/vram/vssd/vgpu,
# поэтому сам список профилей пока НИКАК не участвует в расчёте — только
# принимается и печатается для отладки/проверки формата. Дальше, когда
# будем учитывать профили при подборе конфигурации (не только суммарно),
# raw_profiles уже готов к использованию.
# ---------------------------------------------------------------------------


class VdiProfile(BaseModel):
    name: str
    vcpu: int = Field(..., ge=0)
    vram: int = Field(..., ge=0)
    vssd: int = Field(..., ge=0)
    vgpu: int = Field(..., ge=0)
    count: int = Field(..., ge=0)

    model_config = ConfigDict(extra="forbid")


def _parse_vdi_profiles(raw: str | None) -> list[VdiProfile] | None:
    if raw in (None, ""):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"vdi_profiles: невалидный JSON ({exc})",
        )
    if not isinstance(data, list):
        raise HTTPException(status_code=400, detail="vdi_profiles: ожидается JSON-массив")
    try:
        return [VdiProfile.model_validate(item) for item in data]
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"vdi_profiles: {exc}")


# ---- Модель входных query-параметров ----

class RequestedConfigParams(BaseModel):
    vcpu: int = Field(..., gt=0, description="Количество vCPU")
    vram: int = Field(..., gt=0, description="Объём RAM, ГБ")
    vssd: int = Field(..., gt=0, description="Объём SSD, ГБ")
    vgpu: int = Field(..., ge=0, description="Объём vGPU, ГБ")
    cpu_min_frequency: int = Field(..., gt=0, description="Минимальная частота CPU, МГц")
    cpu_overcommit: int = Field(..., gt=0, description="Overcommit по CPU")
    cpu_vendor: str = Field(default="any", description="Вендор CPU (список — GET /field-options)")
    works_main: str = Field(..., description="Тип ЧО (список — GET /field-options)")
    capacity_disk_type: str = Field(..., description="Тип дисков хранения (список — GET /field-options)")
    # vsan_type: db.VsanType  # pyright: ignore[reportInvalidTypeForm]
    # Приходит только при works_main == "vdi" — сырой JSON-массив профилей
    # рабочих столов (см. VdiProfile выше). Пока не участвует в расчёте
    # requested_config(), см. комментарий у _parse_vdi_profiles().
    vdi_profiles: str | None = Field(
        default=None,
        description="JSON-массив профилей рабочих столов VDI (только при works_main=vdi)",
    )

    model_config = ConfigDict(extra="forbid")


# ---- Модель одного варианта конфигурации в ответе ----

class ConfigOption(BaseModel):
    hosts_by_cpu: int = Field(..., alias="Need hosts by CPU")
    cpu_redundancy: str = Field(..., alias="CPU redundancy")
    all_flash_vsan: str = Field(..., alias="AllFlash vSAN")
    failures_to_tolerate: int = Field(..., alias="Failures to Tolerate")
    cpu_overcommit: str = Field(..., alias="CPU overcommit")
    max_cpu_usage: str = Field(..., alias="Max CPU usage")
    cpu: tuple[str, str] = Field(..., alias="CPU")
    server: str = Field(..., alias="Server")
    videocard: str = Field(..., alias="Videocard")
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
    vgpu_available: str = Field(..., alias="vGPU available")
    total_price_rub: tuple[int, str] = Field(..., alias="Total price, Rub")

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",  # набор полей фиксирован — строгая валидация
        json_schema_extra={
            "example": {
                "Need hosts by CPU": 7,
                "CPU redundancy": "n+1",
                "AllFlash vSAN": "RAID-6",
                "Failures to Tolerate": 2,
                "CPU overcommit": "1",
                "Max CPU usage": "0.85",
                "CPU": ["EPYC 9474F (48x3.6 GHz)", "2 шт"],
                "Server": "ASUS RS720A-E12-RS12 - 1 шт",
                "Videocard": "Nvidia A16 - 1 шт",
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
                "vGPU available": "276",
                "Total price, Rub": [1640841, "руб."],
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
def get_requested_config(params: Annotated[RequestedConfigParams, Query()]) -> list[ConfigOption]:
    vdi_profiles = _parse_vdi_profiles(params.vdi_profiles)

    for field_name, value, allowed in (
        ("cpu_vendor", params.cpu_vendor, db.get_cpu_vendors()),
        ("works_main", params.works_main, db.get_works_main_values()),
        ("capacity_disk_type", params.capacity_disk_type, db.get_capacity_disk_types()),
    ):
        if value not in allowed:
            raise HTTPException(
                status_code=422,
                detail=f"{field_name}: недопустимое значение '{value}'. Допустимые значения: {allowed}",
            )

    raw_result = requested_config(
        db_data=db.load_db_data(),
        vcpu=params.vcpu,
        vram=params.vram,
        vssd=params.vssd,
        vgpu=params.vgpu,
        cpu_min_frequency=params.cpu_min_frequency,
        cpu_overcommit=params.cpu_overcommit,
        cpu_vendor=params.cpu_vendor,
        cloud_type=params.works_main,
        capacity_disk_type=params.capacity_disk_type,
        # vsan_type=params.vsan_type.value,
        vdi_profiles=vdi_profiles,
    )

    return  raw_result