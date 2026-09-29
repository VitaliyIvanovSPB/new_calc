import math
from collections import defaultdict
from typing import Any
import time


def interpolate_coefficient(hosts_qty: int, points: list[tuple[int, float]]) -> float:
    if hosts_qty <= points[0][0]:
        x0, y0 = points[0]
        return y0 / x0 * hosts_qty
    if hosts_qty >= points[-1][0]:
        return points[-1][1]
 
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= hosts_qty <= x1:
            t = (hosts_qty - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)


def get_works_price(hosts_qty: int, works_base: float,
                     works_coefficients: list[tuple[int, float]]) -> float:
    coefficient = interpolate_coefficient(hosts_qty, works_coefficients)
    return works_base * coefficient 

def get_network_price(host_qty: int, network_card_qty: int, switches: float, switches_ipmi: float, switch_ports: int=44) -> float:
    network = (host_qty * network_card_qty * 2) / switch_ports
    network_ipmi = host_qty / switch_ports
    return network * switches + network_ipmi * switches_ipmi

def get_ram_in_host(cpu_hosts: int, ram: dict, vram: int, max_ram_usage: float, unbalanced_ram_module_counts: frozenset[int]) -> int:
    ram_host = math.ceil(vram / max_ram_usage / cpu_hosts / ram["ram_size"])
    ram_host += ram_host % 2  # только чётное число модулей
    if ram_host in unbalanced_ram_module_counts:
        ram_host += 2
    return ram_host

def get_vsan_disks_price(capacity_disk_type: str, disk_size: int, cache_qty: int,
                          capacity_qty: int, cache_disc: dict[str, Any],
                          capacity_disks: dict[str, dict[int, dict[str, Any]]],
                          include_cache_disk: bool = True) -> float:
    host_cache_disks_price = cache_disc["price"] * cache_qty if include_cache_disk else 0
    host_capacity_disks_price = (
        capacity_disks[capacity_disk_type][disk_size]["price"] * capacity_qty * cache_qty
    )
    return host_cache_disks_price + host_capacity_disks_price


def get_raid_disks_capacity(disk_usage_overhead, slack_space, disks_size) -> dict[int, dict[tuple[int, int], int]]:
    """
    Returns, for each raw disk size, the usable vSAN capacity per host for
    every (cache_qty, capacity_qty) combination.
    """
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

def get_nvidia_license_price(vdi_profiles, nvidia_vpc, nvidia_wws):
    nvidia_license = 0
    for profile in vdi_profiles or []:
        if profile.vgpu == 0:
            continue
        if profile.vgpu <= 2:
            nvidia_license += nvidia_vpc * profile.count
        else:
            nvidia_license += nvidia_wws * profile.count    
    return nvidia_license


def requested_config(db_data: dict[str, Any], vcpu: int, vram: int, vssd: int, 
                      cpu_vendor: str, cpu_min_frequency: int, cpu_overcommit: int,
                      cloud_type: str, capacity_disk_type: str,
                      vsan_type: str="osa", network_card_qty: int = 1, vgpu: int=0, vdi_profiles = None):
    
    parameters_float = db_data["parameters_float"]
    unbalanced_ram_module_counts = db_data["unbalanced_ram_module_counts"]
    all_servers = db_data["servers"]
    all_rams = db_data["rams"]
    esxi_disc = db_data["esxi_disks"]
    cache_disc = db_data["cache_disks"][0]
    network_card = db_data["network_cards"][0]
    hba_adapter = db_data["hba_adapters"]
    capacity_disks = db_data["capacity_disks"]
    raid_config = db_data["raid_config"]
    slack_space = db_data["slack_space"][vsan_type]
    videocards = db_data["videocards"]
    all_configs = []
    is_esa = vsan_type == "esa"
    effective_disk_type = "nvme" if is_esa else capacity_disk_type

    filtered_cpus = [
        cpu for cpu in db_data["cpus"]
        if cpu["cores_frequency"] >= cpu_min_frequency
        and (cpu_vendor == "any" or cpu["vendor"] == cpu_vendor)]
    
    nvidia_license = get_nvidia_license_price(vdi_profiles, nvidia_vpc= parameters_float["nvidia_vpc"], nvidia_wws= parameters_float["nvidia_wws"])
    
    # По дефолту max_cpu_usage берётся из БД (parameters_float["max_cpu_usage"], обычно 0.8),
    # но перебираем весь диапазон 0.80..0.90 с шагом 0.01, чтобы найти более дешёвые
    # конфигурации за счёт более плотной утилизации CPU (меньше cpu_hosts при том же vcpu).
    max_cpu_usage_min = parameters_float["max_cpu_usage"]
    max_cpu_usage_values = sorted({
        round(v / 100, 2)
        for v in range(round(max_cpu_usage_min * 100), 91)
    })

    for cpu in filtered_cpus:
            for max_cpu_usage in max_cpu_usage_values:
                cpu_cores_per_host = cpu["cores_quantity"] * 2
                cpu_hosts = math.ceil(vcpu / cpu_overcommit / (cpu_cores_per_host *  max_cpu_usage))
                # if cpu_hosts < 4:
                #     continue

                n = min(5, math.ceil(cpu_hosts / 32))
                cpu_hosts_n = cpu_hosts + n
                key = 6 if cpu_hosts_n >= 6 else (5 if cpu_hosts_n == 5 else 1)
                disk_usage_overhead = raid_config[key]["disk_usage_overhead"]
                vsan_raw = vssd * disk_usage_overhead / (1 - slack_space)

                raids_data = get_raid_disks_capacity(
                    disk_usage_overhead=disk_usage_overhead, slack_space=slack_space, disks_size=capacity_disks[effective_disk_type].keys())

                if vgpu:
                    servers = [s for s in all_servers if s["socket"] == cpu["socket"] and s["gpu_support"]]
                else:
                    servers = [s for s in all_servers if s["socket"] == cpu["socket"]]

                for server in servers:
                    max_ram = server["max_ram"]
                    rams = [r for r in all_rams if r["ram_gen"] == server["ram_gen"]]

                    for ram in rams:
                            ram_1host = get_ram_in_host(cpu_hosts=cpu_hosts, ram= ram, vram= vram, max_ram_usage= parameters_float["max_ram_usage"], 
                                                        unbalanced_ram_module_counts= unbalanced_ram_module_counts)
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

                                    vsan_disks_price = get_vsan_disks_price(
                                        capacity_disk_type=effective_disk_type,
                                        disk_size=disk_size,
                                        cache_qty=cache_qty,
                                        capacity_qty=capacity_qty,
                                        cache_disc=cache_disc,
                                        capacity_disks=capacity_disks,
                                        include_cache_disk=not is_esa,
                                    )

                                    video_iter = videocards if vgpu else (None,)
                                    for videocard in video_iter:
                                        if vgpu:
                                            videocards_per_host = math.ceil(vgpu / cpu_hosts / videocard["vram"])
                                            if server["gpu_size"] < videocard["size"] * videocards_per_host:
                                                continue
                                            gpu_price = videocard["price"] * videocards_per_host
                                            vgpu_available = math.ceil(videocards_per_host * cpu_hosts* videocard["vram"])
                                        else:
                                            videocards_per_host = 0
                                            gpu_price = 0
                                            vgpu_available = 0

                                    
                                        host_price = (
                                            cpu["price"] * 2 + server["price"] + ram_1host * ram["price"]
                                            + esxi_disc[effective_disk_type]["price"] + network_card["price"]
                                            + vsan_disks_price + (hba["price"] if hba else 0) 
                                            + gpu_price)
                                    
                                        host_rms = math.ceil(
                                            host_price * parameters_float["currency"] / parameters_float["payback_period"]
                                            * parameters_float["coefficient_vmware"])
                                    
                                        cluster_rms = host_rms * cpu_hosts_n
                                    
                                        works = math.ceil(get_works_price(
                                            hosts_qty=cpu_hosts_n,
                                            works_base=parameters_float["works_base"],
                                            works_coefficients=db_data["works_coefficient"][cloud_type],
                                            )
                                        )

                                        network_price = math.ceil(
                                            get_network_price(
                                                host_qty=cpu_hosts_n, network_card_qty=network_card_qty,
                                                switches=parameters_float.get("switches"), switches_ipmi=parameters_float.get("switches_ipmi"),
                                                switch_ports=parameters_float["switch_ports"]
                                            )
                                        )
                                    
                                        cluster_cores_qty =  cpu_cores_per_host * cpu_hosts_n 
                                    
                                        vmware_license = cluster_cores_qty * parameters_float["vcf"] * parameters_float["vcf_to_client"] * parameters_float["currency"]
                                    
                                        vsan_addon_license = math.ceil(
                                            max(0.0, (vssd / 1024) - cluster_cores_qty)) * parameters_float["currency"] * parameters_float["vsan_addonn_1TB"] * parameters_float["vsan_addon_to_client"]

                                        total_price = cluster_rms + works + network_price + vmware_license + vsan_addon_license + (nvidia_license * parameters_float["currency"])

                                        vcpu_available = math.ceil(
                                            cpu_cores_per_host * cpu_hosts * cpu_overcommit * max_cpu_usage)
                                    
                                        vram_available = math.ceil(
                                            ram_1host * ram["ram_size"] * cpu_hosts * parameters_float["max_ram_usage"])
                                    
                                        all_configs.append(
                                            {
                                                "Need hosts by CPU": cpu_hosts_n,
                                                "CPU redundancy": f"n+{n}",
                                                "AllFlash vSAN": raid_config[key]["FTM"],
                                                "Failures to Tolerate": raid_config[key]["FTT"],
                                                "CPU overcommit": f"{cpu_overcommit}",
                                                "Max CPU usage": f"{max_cpu_usage}",
                                                "CPU": [cpu["name"], "2 шт"],
                                                "Server": f'{server["name"]} - 1 шт',
                                                "Videocard": f'{videocard["name"]} - {videocards_per_host} шт' if vgpu else "-",
                                                "RAM": f'{ram["ram_size"]}Gb {ram["ram_gen"]} - {ram_1host} шт',
                                                "Esxi disk": f'{esxi_disc[effective_disk_type]["disk_type"]} - 1 шт',
                                                "Cache disk": (
                                                    "—" if is_esa
                                                    else f'{cache_disc["capacity"]} {cache_disc["disk_type"]} - {cache_qty} шт'),
                                                "Capacity disk": f'{disk_size} {effective_disk_type} - {capacity_qty * cache_qty} шт',
                                                "Network card": f'{network_card["name"]} - {network_card_qty} шт',
                                                "HBA adapter": f'{hba["name"]} - 1 шт' if hba else "-",
                                                "Admin main works": f"{cloud_type}",
                                                "vCPU available": f"{vcpu_available}",
                                                "vRAM available": f"{vram_available}",
                                                "vSSD available": f"{disks_capacity * cpu_hosts}",
                                                "vGPU available": f"{vgpu_available} , кластер-{cluster_rms}, работы-{works}, сеть-{network_price}, варя-{vmware_license}, {vsan_addon_license}, нвидиа-({nvidia_license} * {parameters_float['currency']})",
                                                "Total price, Rub": [int(total_price), f"руб."],
                                            }
                                        )

    # Группируем конфигурации по имени процессора и выбираем самую дешевую для каждого
    grouped_by_cpu = defaultdict(list)
    for config in all_configs:
        cpu_name = config["CPU"][0]
        grouped_by_cpu[cpu_name].append(config)

    cheapest_per_cpu = []
    for configs_list in grouped_by_cpu.values():
        sorted_configs = sorted(
            configs_list, key=lambda x: int(x["Total price, Rub"][0])
        )
        cheapest_per_cpu.append(sorted_configs[0])

    sorted_configs = sorted(
        cheapest_per_cpu, key=lambda x: int(x["Total price, Rub"][0])
    )
    return sorted_configs




if __name__ == "__main__":
    # Локальная сборка db_data напрямую из SQLite — только для ручного
    # прогона файла. В production данные приходят из main.py:load_db_data().
    from db import load_db_data

    start = time.perf_counter()

    print(
        requested_config(
            db_data=load_db_data(),
            vcpu=60385,
            vram=162418,
            vssd=1500000,
            vgpu=150,
            cpu_min_frequency=4000,
            cpu_overcommit=8,
            cpu_vendor="amd",
            network_card_qty=1,
            cloud_type="vsphere",
            capacity_disk_type="nvme",
            vsan_type="esa",
        )
    )

    end = time.perf_counter()

    print(f"Время: {end - start:.4f} сек")