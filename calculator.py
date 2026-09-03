import math
from collections import defaultdict
from typing import Any
import time


# ---------------------------------------------------------------------------
# Pure calculation helpers
#
# calculator.py больше не обращается к БД напрямую: все справочные данные
# (parameters, cpus, servers, rams, диски, raid_config и т.д.) приходят
# извне единым словарём db_data — см. main.py:load_db_data().
# ---------------------------------------------------------------------------


def get_works_price(hosts_qty: int, main: str, works_base: float,
                     works_coefficients: dict[Any, tuple[Any, ...]]) -> float:
    thresholds = [8, 16, 24, 32, 64, 96, 128]
    index = next((i for i, limit in enumerate(thresholds) if hosts_qty < limit), 6)
    return works_base * works_coefficients[main][index]/thresholds[6]*hosts_qty


def get_network_coefficient(value, ports_map):
    return next((v for k, v in ports_map.items() if value <= k), 9)


def get_network_price(host_qty: int, network_card_qty: int, switches, switches_ipmi):
    switch_ports_map = {
        6: 0.125,
        12: 0.25,
        24: 0.5,
        48: 1,
        72: 1.5,
        96: 2,
        120: 2.5,
        144: 3,
        168: 3.5,
        192: 4,
        250: 5,
        298: 6,
        346: 7,
        384: 8,
    }
    ipmi_switch_ports_map = {
        6: 0.125,
        12: 0.25,
        24: 0.5,
        48: 1,
        96: 2,
        144: 3,
        240: 4,
    }
    network = get_network_coefficient(host_qty * network_card_qty * 2, switch_ports_map)
    network_ipmi = get_network_coefficient(host_qty, ipmi_switch_ports_map)
    print(network )
    print(host_qty * network_card_qty * 2)
    print(network_ipmi )

    return network * switches + network_ipmi * switches_ipmi


def get_ram_in_host(cpu_hosts, ram, vram, max_ram_usage):
    ram_host = math.ceil(vram / max_ram_usage / cpu_hosts / ram["ram_size"])
    return ram_host + (ram_host % 2)


def get_vsan_disks_price(capacity_disk_type: str, disk_size: int, cache_qty: int,
                          capacity_qty: int, cache_disc: dict[str, Any],
                          capacity_disks: dict[str, dict[int, dict[str, Any]]],
                          include_cache_disk: bool = True) -> float:
    host_cache_disks_price = cache_disc["price"] * cache_qty if include_cache_disk else 0
    host_capacity_disks_price = (
        capacity_disks[capacity_disk_type][disk_size]["price"] * capacity_qty * cache_qty
    )
    return host_cache_disks_price + host_capacity_disks_price


def get_raid_disks_capacity(disk_usage_overhead, slack_space) -> dict[int, dict[tuple[int, int], int]]:
    """
    Returns, for each raw disk size, the usable vSAN capacity per host for
    every (cache_qty, capacity_qty) combination.
    """
    disks_size = [960, 1920, 3840, 7680, 15360]
    raid_disks_capacity: dict[int, dict[tuple[int, int], int]] = {}
    for disk in disks_size:
        raid_disks_capacity[disk] = {}
        for cache_qty in range(2, 6):
            for capacity_qty in range(2, 8):
                size = int(
                    disk * cache_qty * capacity_qty * (1 - slack_space) / disk_usage_overhead
                )
                raid_disks_capacity[disk][(cache_qty, capacity_qty)] = size
    return raid_disks_capacity


def check_vsan_and_disks_limit(cpu_hosts, disks_capacity, host_disks_qty,
                                server, vsan_raw, vssd):
    min_vsan_condition = vssd <= disks_capacity * cpu_hosts
    max_vsan_condition = disks_capacity * cpu_hosts < vsan_raw * 1.2
    disks_limit_condition = host_disks_qty <= server["max_disks_qty"]
    return min_vsan_condition and max_vsan_condition and disks_limit_condition


# ---------------------------------------------------------------------------
# Main sizing routine
# ---------------------------------------------------------------------------


def requested_config(db_data: dict[str, Any], vcpu: int, vram: int, vssd: int,
                      cpu_vendor: str, cpu_min_frequency: int, cpu_overcommit: float,
                      works_main: str, capacity_disk_type: str,
                      vsan_type: str="osa", network_card_qty: int = 1):
    parameters = db_data["parameters"]
    all_servers = db_data["servers"]
    all_rams = db_data["rams"]
    esxi_disc = db_data["esxi_disks"]
    cache_disc = db_data["cache_disks"][0]
    network_card = db_data["network_cards"][0]
    hba_adapter = db_data["hba_adapters"]
    capacity_disks = db_data["capacity_disks"]
    raid_config = db_data["raid_config"]
    works_coefficients = db_data["works_coefficient"]
    slack_space = db_data["slack_space"][vsan_type]
    all_configs = []

    is_esa = vsan_type == "esa"
    effective_disk_type = "nvme" if is_esa else capacity_disk_type

    filtered_cpus = [
        cpu for cpu in db_data["cpus"]
        if cpu["cores_frequency"] >= cpu_min_frequency
        and (cpu_vendor == "any" or cpu["vendor"] == cpu_vendor)
    ]

    for cpu in filtered_cpus:
        cpu_hosts = math.ceil(
            vcpu / cpu_overcommit / (cpu["cores_quantity"] * 2 * parameters["max_cpu_usage"])
        )
        if cpu_hosts < 4:
            continue

        n = min(5, math.ceil(cpu_hosts / 32))
        cpu_hosts_n = cpu_hosts + n
        key = 6 if cpu_hosts_n >= 6 else (5 if cpu_hosts_n == 5 else 1)
        disk_usage_overhead = raid_config[key]["disk_usage_overhead"]
        vsan_raw = vssd * disk_usage_overhead / (1 - slack_space)

        raids_data = get_raid_disks_capacity(
            disk_usage_overhead=disk_usage_overhead, slack_space=slack_space
        )

        servers = [s for s in all_servers if s["socket"] == cpu["socket"]]
        for server in servers:
            max_ram = server["max_ram"]
            rams = [r for r in all_rams if r["ram_gen"] == server["ram_gen"]]

            for ram in rams:
                ram_1host = get_ram_in_host(cpu_hosts, ram, vram, parameters["max_ram_usage"])
                if ram_1host in (14, 18, 22):
                    ram_1host += 2
                if ram_1host > max_ram:
                    continue

                for disk_size, disk_groups in raids_data.items():
                    for (cache_qty, capacity_qty), disks_capacity in disk_groups.items():
                        hba = None

                        if is_esa:
                            host_disks_qty = cache_qty * capacity_qty
                        else:
                            host_disks_qty = cache_qty * capacity_qty + cache_qty
                            host_disks_hba_qty = cache_qty * capacity_qty

                            if effective_disk_type == "ssd":
                                hba = hba_adapter[8] if host_disks_hba_qty < 9 else hba_adapter[16]

                        nvme_disks_qty = host_disks_qty if effective_disk_type == "nvme" else cache_qty

                        if nvme_disks_qty > server["max_nvme_disks_qty"]:
                            continue

                        vsan_and_disks_limit = check_vsan_and_disks_limit(
                            cpu_hosts, disks_capacity, host_disks_qty, server, vsan_raw, vssd)

                        if not vsan_and_disks_limit:
                            continue

                        if disk_size == 15360 and effective_disk_type == "ssd":
                            continue

                        vsan_disks_price = get_vsan_disks_price(
                            capacity_disk_type=effective_disk_type,
                            disk_size=disk_size,
                            cache_qty=cache_qty,
                            capacity_qty=capacity_qty,
                            cache_disc=cache_disc,
                            capacity_disks=capacity_disks,
                            include_cache_disk=not is_esa,
                        )
                        host_price = (
                            cpu["price"] * 2 + server["price"] + ram_1host * ram["price"]
                            + esxi_disc[effective_disk_type]["price"] + network_card["price"]
                            + vsan_disks_price + (hba["price"] if hba else 0)
                        )
                        rms = math.ceil(
                            host_price * parameters["currency"] / parameters["payback_period"]
                            * parameters["coefficient"]
                        )
                        vmware = rms * cpu_hosts_n
                        works = math.ceil(
                            get_works_price(
                                hosts_qty=cpu_hosts_n, main=works_main,
                                works_base=parameters["works_base"],
                                works_coefficients=works_coefficients,
                            )
                        )

                        switches = parameters.get("switches")
                        switches_ipmi = parameters.get("switches_ipmi")
                        if switches is None or switches_ipmi is None:
                            continue
                        network_price = math.ceil(
                            get_network_price(
                                host_qty=cpu_hosts_n, network_card_qty=network_card_qty,
                                switches=switches, switches_ipmi=switches_ipmi,
                            )
                        )

                        total_price = vmware + works + network_price
                        vcpu_available = math.ceil(
                            cpu["cores_quantity"] * 2 * cpu_hosts * cpu_overcommit
                            * parameters["max_cpu_usage"]
                        )
                        vram_available = math.ceil(
                            ram_1host * ram["ram_size"] * cpu_hosts * parameters["max_ram_usage"]
                        )
                        all_configs.append(
                            {
                                "Need hosts by CPU": cpu_hosts_n,
                                "CPU redundancy": f"n+{n}",
                                "AllFlash vSAN": raid_config[key]["FTM"],
                                "Failures to Tolerate": raid_config[key]["FTT"],
                                "CPU overcommit": f"{cpu_overcommit}",
                                "CPU": f'{cpu["name"]} - 2 шт',
                                "Server": f'{server["name"]} - 1 шт',
                                "RAM": f'{ram["ram_size"]}Gb {ram["ram_gen"]} {ram_1host} шт',
                                "Esxi disk": f'{esxi_disc[effective_disk_type]["disk_type"]} - 1 шт',
                                "Cache disk": (
                                    "—" if is_esa
                                    else f'{cache_disc["capacity"]} {cache_disc["disk_type"]} - {cache_qty} шт'
                                ),
                                "Capacity disk": f'{disk_size} {effective_disk_type} - {capacity_qty * cache_qty} шт',
                                "Network card": f'{network_card["name"]} - {network_card_qty} шт',
                                "HBA adapter": f'{hba["name"]} - 1 шт' if hba else "-",
                                "Admin main works": f"{works_main}",
                                "vCPU available": f"{vcpu_available}",
                                "vRAM available": f"{vram_available}",
                                "vSSD available": f"{disks_capacity * cpu_hosts}",
                                "Total price, Rub": f"{total_price} руб.",
                            }
                        )

    # Группируем конфигурации по имени процессора и выбираем самую дешевую для каждого
    grouped_by_cpu = defaultdict(list)
    for config in all_configs:
        cpu_name = config["CPU"].split(" - ")[0]
        grouped_by_cpu[cpu_name].append(config)

    cheapest_per_cpu = []
    for configs_list in grouped_by_cpu.values():
        sorted_configs = sorted(
            configs_list, key=lambda x: int(x["Total price, Rub"].split(" ")[0])
        )
        cheapest_per_cpu.append(sorted_configs[0])

    sorted_configs = sorted(
        cheapest_per_cpu, key=lambda x: int(x["Total price, Rub"].split(" ")[0])
    )
    return sorted_configs


if __name__ == "__main__":
    # Локальная сборка db_data напрямую из SQLite — только для ручного
    # прогона файла. В production данные приходят из main.py:load_db_data().
    import sqlite3

    def _load_db_data_for_test(db_path: str = "data.db") -> dict[str, Any]:
        with sqlite3.connect(db_path) as conn:
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
                    "vendor": row[0], "name": row[1], "cores_quantity": row[2],
                    "cores_frequency": row[3], "price": row[4], "socket": row[5],
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
                    "name": row[0], "cpu_vendor": row[1], "socket": row[2],
                    "max_ram": row[3], "ram_gen": row[4],
                    "max_disks_qty": int(row[5]), "max_nvme_disks_qty": int(row[6]),
                    "price": row[7],
                }
                for row in cursor.fetchall()
            )

            cursor.execute("SELECT disk_type, capacity, price FROM esxi_disks")
            esxi_disks = {row[0]: {"disk_type": row[1], "price": row[2]} for row in cursor.fetchall()}

            cursor.execute("SELECT disk_type, capacity, price FROM cache_disks")
            cache_disks = tuple(
                {"disk_type": row[0], "capacity": row[1], "price": row[2]}
                for row in cursor.fetchall()
            )

            cursor.execute("SELECT name, price FROM network_cards")
            network_cards = tuple({"name": row[0], "price": row[1]} for row in cursor.fetchall())

            cursor.execute("SELECT spec, name, price FROM hba_adapters")
            hba_adapters = {row[0]: {"name": row[1], "price": row[2]} for row in cursor.fetchall()}

            cursor.execute("SELECT disk_type, capacity, price FROM capacity_disks")
            capacity_disks: dict[str, dict[int, dict[str, Any]]] = {"ssd": {}, "nvme": {}}
            for row in cursor.fetchall():
                capacity_disks[row[0]][row[1]] = {"price": row[2]}

            cursor.execute("SELECT ram_gen, ram_size, price FROM rams")
            rams = tuple(
                {"ram_gen": row[0], "ram_size": row[1], "price": row[2]}
                for row in cursor.fetchall()
            )

            cursor.execute("SELECT raid_level, FTM, FTT, disk_usage_overhead FROM raid_config")
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

    start = time.perf_counter()

    print(
        requested_config(
            db_data=_load_db_data_for_test(),
            vcpu=60385,
            vram=162418,
            vssd=1500000,
            cpu_min_frequency=2400,
            cpu_overcommit=8,
            cpu_vendor="amd",
            network_card_qty=1,
            works_main="vsphere",
            capacity_disk_type="nvme",
            vsan_type="esa",
        )
    )

    end = time.perf_counter()

    print(f"Время: {end - start:.4f} сек")