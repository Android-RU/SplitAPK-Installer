#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Умный инсталлятор Split APK пакетов для Android-устройств через ADB.
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import List, Dict, Any, Optional

log = logging.getLogger("split_apk_installer")


# -----------------------
# Утилиты работы с ADB
# -----------------------

def run_adb(args: List[str], serial: Optional[str] = None, check=True, capture=True) -> subprocess.CompletedProcess:
    """Запуск команды adb с обработкой stdout/stderr"""
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    log.debug("ADB CMD: %s", " ".join(cmd))
    return subprocess.run(cmd,
                         check=check,
                         capture_output=capture,
                         text=True)


def detect_device(serial: Optional[str] = None) -> Dict[str, Any]:
    """Снятие параметров устройства через adb"""
    props = {}
    def prop(name: str) -> str:
        try:
            out = run_adb(["shell", "getprop", name], serial).stdout.strip()
            return out
        except subprocess.CalledProcessError:
            return ""

    abis = prop("ro.product.cpu.abilist")
    if not abis:
        abis = prop("ro.product.cpu.abi")
    sdk = prop("ro.build.version.sdk")
    density = prop("ro.sf.lcd_density") or ""
    locale = prop("persist.sys.locale") or prop("ro.product.locale")

    return {
        "serial": serial,
        "sdk": int(sdk) if sdk.isdigit() else None,
        "abis": abis.split(",") if abis else [],
        "density": int(density) if density.isdigit() else None,
        "locales": [locale] if locale else []
    }


# -----------------------
# Работа с APK
# -----------------------

def collect_inputs(paths: List[str], tempdir: str) -> List[Path]:
    """Собирает все APK из файлов/директорий/архивов .apks/.xapk"""
    apk_files = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            apk_files += list(path.glob("*.apk"))
        elif path.suffix.lower() == ".apk":
            apk_files.append(path)
        elif path.suffix.lower() in (".apks", ".xapk"):
            z = zipfile.ZipFile(path, "r")
            extract_dir = Path(tempdir) / path.stem
            z.extractall(extract_dir)
            apk_files += list(extract_dir.rglob("*.apk"))
        else:
            log.warning("Неизвестный формат файла: %s", path)
    return apk_files


def classify_apk(path: Path) -> Dict[str, Any]:
    """
    Классификация apk-файла: base или config (abi/dpi/lang/feature).
    Для простоты используем имя файла. В реальной версии можно читать AndroidManifest.xml.
    """
    name = path.name
    info = {
        "path": str(path),
        "type": "unknown",
        "value": None,
        "package": None,
        "versionCode": None
    }
    if name == "base.apk":
        info["type"] = "base"
    elif "config." in name:
        # config.arm64_v8a.apk, config.ru.apk, config.xxhdpi.apk
        m = re.search(r"config\.([^.]+)", name)
        if m:
            value = m.group(1)
            info["type"] = "config"
            info["value"] = value
    elif name.startswith("feature_") or "feature" in name:
        info["type"] = "feature"
    return info


def group_by_package(apks: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Пока не читаем реально packageName/versionCode, группируем по base.
    Можно расширить через парсинг манифеста.
    """
    groups = {"default": apks}
    return groups


# -----------------------
# Алгоритм выбора сплитов
# -----------------------

def select_splits(apks: List[Dict[str, Any]], device: Dict[str, Any], opts: argparse.Namespace):
    """Простейший выбор: base + подходящие abi/dpi/lang"""
    base = [a for a in apks if a["type"] == "base"]
    if not base:
        raise RuntimeError("Не найден base.apk")
    selection = base.copy()

    # ABI
    if device["abis"]:
        for abi in device["abis"]:
            for a in apks:
                if a["type"] == "config" and abi in (a.get("value") or ""):
                    selection.append(a)
                    break
            else:
                continue
            break

    # DPI
    if device["density"]:
        density = device["density"]
        for a in apks:
            if a["type"] == "config" and str(density) in (a.get("value") or ""):
                selection.append(a)
                break

    # LANG
    if device["locales"]:
        lang = device["locales"][0].split("-")[0]
        for a in apks:
            if a["type"] == "config" and lang == a.get("value"):
                selection.append(a)
                break

    return selection


# -----------------------
# Установка
# -----------------------

def run_install(selection: List[Dict[str, Any]], device: Dict[str, Any], opts: argparse.Namespace) -> Dict[str, Any]:
    """Выполняет установку через adb install-multiple"""
    files = [a["path"] for a in selection]
    cmd = ["install-multiple"]
    if opts.replace:
        cmd.append("-r")
    if opts.downgrade:
        cmd.append("-d")
    if opts.grant_all:
        cmd.append("-g")
    if opts.staged:
        cmd.append("--staged")
    cmd += files

    if opts.dry_run:
        log.info("Dry-run: adb %s", " ".join(cmd))
        return {"exit_code": 0, "message": "Dry-run, установка не выполнялась"}

    try:
        res = run_adb(cmd, device["serial"], check=False)
        return {"exit_code": res.returncode, "message": res.stdout + res.stderr}
    except subprocess.CalledProcessError as e:
        return {"exit_code": e.returncode, "message": str(e)}


# -----------------------
# Основной сценарий
# -----------------------

def main():
    parser = argparse.ArgumentParser(description="Умный инсталлятор Split APK для Android")
    parser.add_argument("sources", nargs="+", help="Файлы/директории (.apk/.apks/.xapk)")
    parser.add_argument("--device", help="Серийный номер устройства")
    parser.add_argument("--dry-run", action="store_true", help="Только печать команды")
    parser.add_argument("-r", "--replace", action="store_true", help="Разрешить замену установленного")
    parser.add_argument("-d", "--downgrade", action="store_true", help="Разрешить даунгрейд")
    parser.add_argument("-g", "--grant-all", action="store_true", help="Грантить все разрешения")
    parser.add_argument("--staged", action="store_true", help="Staged install (Android 10+)")
    parser.add_argument("--json", action="store_true", help="Вывод в JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="Подробный вывод")
    opts = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if opts.verbose else logging.INFO,
                        format="%(levelname)s: %(message)s")

    with tempfile.TemporaryDirectory() as tmp:
        apk_files = collect_inputs(opts.sources, tmp)
        if not apk_files:
            log.error("Не найдено ни одного APK")
            sys.exit(2)

        apks = [classify_apk(p) for p in apk_files]
        groups = group_by_package(apks)

        # Пока берём "default" группу
        device = detect_device(opts.device)
        try:
            selection = select_splits(groups["default"], device, opts)
        except Exception as e:
            log.error("Ошибка выбора сплитов: %s", e)
            sys.exit(3)

        plan = {
            "device": device,
            "selected_apks": selection,
        }

        result = run_install(selection, device, opts)
        plan["result"] = result

        if opts.json:
            print(json.dumps(plan, indent=2, ensure_ascii=False))
        else:
            print("Устройство:", device)
            print("Выбранные APK:")
            for a in selection:
                print(" -", a["path"], a["type"], a.get("value"))
            print("Результат:", result)


if __name__ == "__main__":
    main()