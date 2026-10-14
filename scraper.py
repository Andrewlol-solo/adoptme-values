import json
import math
import os
import re
import shutil
import sys
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://amvgg.com"
# раздел сайта -> (путь, ключ категории в values_*.json)
SECTIONS = {
    "pets": ("/values/pets", "Pets"),
    "eggs": ("/values/eggs", "Eggs"),
    "petwear": ("/values/petwear", "Petwear"),
    "strollers": ("/values/strollers", "Strollers"),
    "food": ("/values/food", "Food"),
    "vehicles": ("/values/vehicles", "Vehicles"),
    "toys": ("/values/toys", "Toys"),
    "gifts": ("/values/gifts", "Gifts"),
    "stickers": ("/values/stickers", "Stickers"),
    "houses": ("/values/houses", "Houses"),
}
CALCULATOR_PAGE = "/calculator"  # основной каталог всех предметов и variant-specific values

# Файл, variant_key (как в AMTradeCalc.lua), поля сайта по приоритету.
# Для питомцев regularValue/neonValue/megaValue — стандартная оценка.
# Явные np*/fValue/rValue важнее. Если в источнике вообще нет ни одной отдельной
# оценки вариантов этой формы, используем единственную опубликованную цену как общую.
# Если часть вариантов задана, а нужный вариант пропущен, цену не угадываем.
VARIANTS = [
    ("values_base.json",    "Base",    ["npRegularValue", "regularValue"]),
    ("values_f.json",       "F",       ["fValue", "regularValue"]),
    ("values_r.json",       "R",       ["rValue", "regularValue"]),
    ("values_fr.json",      "FR",      ["regularValue"]),
    ("values_neon.json",    "Neon",    ["npNeonValue", "neonValue"]),
    ("values_neon_f.json",  "Neon|F",  ["nfValue", "neonValue"]),
    ("values_neon_r.json",  "Neon|R",  ["nrValue", "neonValue"]),
    ("values_neon_fr.json", "Neon|FR", ["neonValue"]),
    ("values_mega.json",    "Mega",    ["npMegaValue", "megaValue"]),
    ("values_mega_f.json",  "Mega|F",  ["mfValue", "megaValue"]),
    ("values_mega_r.json",  "Mega|R",  ["mrValue", "megaValue"]),
    ("values_mega_fr.json", "Mega|FR", ["megaValue"]),
]

ALL_FILE = "values_all.json"
GIT_REMOTE = "origin"
EXPECTED_GITHUB_REMOTE = "https://github.com/Andrewlol-solo/adoptme-values"
MIN_RATIO = 0.7
PET_VARIANT_SIBLINGS = {
    "npRegularValue": ("fValue", "rValue"),
    "fValue": ("npRegularValue", "rValue"),
    "rValue": ("npRegularValue", "fValue"),
    "npNeonValue": ("nfValue", "nrValue"),
    "nfValue": ("npNeonValue", "nrValue"),
    "nrValue": ("npNeonValue", "nfValue"),
    "npMegaValue": ("mfValue", "mrValue"),
    "mfValue": ("npMegaValue", "mrValue"),
    "mrValue": ("npMegaValue", "mfValue"),
}
SCRIPT_DIR = Path(__file__).resolve().parent
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
PUSH_RE = re.compile(r"self\.__next_f\.push\((\[.*?\])\)</script>", re.S)
ITEM_START = re.compile(r'\{"id":"[0-9a-f]{24}","name":"')
EXPECT_RE = re.compile(r"Showing all(?:\s|<!--.*?-->)*(\d+)")
decoder = json.JSONDecoder()


def run_git(args, cwd, check=True):
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace"
        )
    except FileNotFoundError as e:
        raise RuntimeError("Git не найден. Установи Git и добавь его в PATH.") from e

    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        # Не выводим учётные данные, если remote настроен через URL с токеном.
        detail = re.sub(r"(https?://)[^/@\s]+@", r"\1***@", detail)
        raise RuntimeError(detail or f"git {' '.join(args[:2])} завершился с ошибкой")
    return result


def normalize_remote(url):
    value = url.strip()
    value = re.sub(r"^git@github\.com:", "https://github.com/", value, flags=re.I)
    value = re.sub(r"^ssh://git@github\.com/", "https://github.com/", value, flags=re.I)
    if value.lower().endswith(".git"):
        value = value[:-4]
    return value.rstrip("/").lower()


def github_target():
    # The double-click launcher and scheduled task live in ../scraper, while the
    # user's GitHub clone is its sibling ../adoptme-values. Find that exact repo
    # instead of requiring the launcher folder itself to be a Git checkout.
    candidates = (SCRIPT_DIR, SCRIPT_DIR.parent / "adoptme-values")
    seen = set()
    for candidate in candidates:
        try:
            root = Path(run_git(["rev-parse", "--show-toplevel"], candidate).stdout.strip()).resolve()
        except RuntimeError:
            continue
        if root in seen:
            continue
        seen.add(root)
        try:
            remote = run_git(["remote", "get-url", "--push", GIT_REMOTE], root).stdout.strip()
        except RuntimeError:
            continue
        if normalize_remote(remote) != EXPECTED_GITHUB_REMOTE.lower():
            continue
        try:
            branch = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], root).stdout.strip()
        except RuntimeError as e:
            raise RuntimeError("Git находится в detached HEAD; переключись на ветку перед запуском.") from e
        if branch:
            return root, branch
    raise RuntimeError(
        "не найден клон GitHub Andrewlol-solo/adoptme-values. Ожидается папка "
        "adoptme-values рядом с папкой scraper и настроенный remote origin."
    )


def publish_to_github(stamp, repo_root, branch):
    generated_files = [ALL_FILE, *(fname for fname, _, _ in VARIANTS)]
    repo_files = ["scraper.py", *generated_files]
    if SCRIPT_DIR.resolve() != repo_root.resolve():
        try:
            for filename in repo_files:
                source = SCRIPT_DIR / filename
                if not source.is_file():
                    raise RuntimeError(f"не найден файл для публикации {filename}.")
                shutil.copy2(source, repo_root / filename)
        except OSError as e:
            raise RuntimeError(f"не удалось скопировать парсер и файлы оценок в GitHub-клон: {e}") from e
        print("GitHub: парсер и файлы оценок скопированы в клон репозитория.")
    paths = repo_files

    run_git(["add", "--", *paths], repo_root)
    diff = run_git(["diff", "--cached", "--quiet", "--", *paths], repo_root, check=False)
    if diff.returncode == 0:
        print("GitHub: файлы оценок не изменились, коммит и отправка не нужны.")
        return
    if diff.returncode != 1:
        raise RuntimeError("не удалось проверить подготовленные изменения Git.")

    commit_message = f"Update Adopt Me values ({stamp})"
    commit_args = ["commit", "-m", commit_message, "--", *paths]
    author = run_git(["var", "GIT_AUTHOR_IDENT"], repo_root, check=False)
    if author.returncode:
        # Use a clearly named automation identity without changing global Git config.
        commit_args = [
            "-c", "user.name=Adopt Me Values Scraper",
            "-c", "user.email=scraper@users.noreply.github.com",
            *commit_args,
        ]
    run_git(commit_args, repo_root)
    run_git(["push", GIT_REMOTE, f"HEAD:refs/heads/{branch}"], repo_root)
    print(f"GitHub: файлы оценок отправлены в {GIT_REMOTE}/{branch}.")


def fetch(path):
    for attempt in range(3):
        try:
            r = requests.get(BASE + path, headers=HEADERS, timeout=40)
            r.raise_for_status()
            return r.text
        except Exception as e:
            print(f"  {path}: попытка {attempt + 1} не удалась ({e})")
            time.sleep(3)
    return ""


def extract_items(html):
    parts = []
    for m in PUSH_RE.finditer(html):
        try:
            arr = json.loads(m.group(1))
        except Exception:
            continue
        if len(arr) > 1 and arr[0] == 1 and isinstance(arr[1], str):
            parts.append(arr[1])
    text = "".join(parts)
    found = {}
    for m in ITEM_START.finditer(text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except Exception:
            continue
        if isinstance(obj, dict) and "name" in obj:
            found[obj["id"]] = obj
    return list(found.values())


def to_num(v):
    if isinstance(v, bool):
        return None
    try:
        number = float(v)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def pick(item, fields):
    if item.get("_category") == "Pets":
        if not fields:
            return None
        value = to_num(item.get(fields[0]))
        if value is not None:
            return value
        # A missing field can mean the source publishes only one common value for
        # that form. Reuse it only when none of the sibling potion variants exist.
        # If the source has partial variant data, leave this variant unpriced instead
        # of silently substituting a potentially incorrect FR/default value.
        siblings = PET_VARIANT_SIBLINGS.get(fields[0])
        if siblings and not any(to_num(item.get(key)) is not None for key in siblings):
            return to_num(item.get(fields[1])) if len(fields) > 1 else None
        return None
    for f in fields:
        v = to_num(item.get(f))
        if v is not None:
            return v
    return to_num(item.get("value"))


def old_total():
    try:
        with open("values_base.json", encoding="utf-8") as f:
            cats = json.load(f).get("categories", {})
        return sum(len(v) for v in cats.values())
    except Exception:
        return 0


def main():
    os.chdir(SCRIPT_DIR)
    try:
        repo_root, branch = github_target()
    except RuntimeError as e:
        print(f"СТОП: публикация в GitHub не настроена: {e}")
        sys.exit(1)

    items, expected = {}, {}
    failed_sections = []
    # The calculator is the user's chosen source of truth and contains the full catalog.
    # Fetch it first so its non-empty values win; section pages then fill missing metadata
    # and assign readable category names. This also preserves section-page fallback if the
    # calculator temporarily fails to load.
    pages = [("calculator", CALCULATOR_PAGE, "Other")] + [
        (s, p, c) for s, (p, c) in SECTIONS.items()
    ]
    for section, path, category in pages:
        html = fetch(path)
        found = extract_items(html)
        if section in SECTIONS and not found:
            failed_sections.append(section)
        m = EXPECT_RE.search(html)
        if m and section in SECTIONS:
            expected[section] = int(m.group(1))
        new, enriched = 0, 0
        for it in found:
            previous = items.get(it["id"])
            if previous is None:
                it["_category"] = category
                items[it["id"]] = it
                new += 1
                continue

            # Calculator values have precedence. Section pages only fill fields that
            # the calculator omitted, while still providing the readable category.
            changed = False
            for key, value in it.items():
                if key == "_category":
                    continue
                if value is not None and previous.get(key) is None:
                    previous[key] = value
                    changed = True
            if category != "Other" and previous.get("_category") != category:
                previous["_category"] = category
                changed = True
            if changed:
                enriched += 1
        print(f"[{section}] на странице {len(found)}, новых {new}, дополнено {enriched}"
              + (f", сайт заявляет {expected[section]}" if section in expected else ""))

    by_cat = Counter(i["_category"] for i in items.values())
    short = [s for s, n in expected.items() if by_cat.get(SECTIONS[s][1], 0) < n]
    if short:
        print("ВНИМАНИЕ, собрано меньше, чем заявлено:", ", ".join(short))
    if failed_sections:
        print("СТОП: не удалось прочитать разделы", ", ".join(failed_sections), ". Файлы не публикуются.")
        sys.exit(1)
    if not items:
        print("СТОП: ничего не найдено, формат страницы мог измениться.")
        sys.exit(1)
    prev = old_total()
    # Validate catalog extraction against the site's regular/FR field, not Base:
    # some pet variants share one common source value while others have explicit variants.
    source_price_total = len([1 for it in items.values() if pick(it, VARIANTS[3][2]) is not None])
    if prev and source_price_total < prev * MIN_RATIO:
        print(f"СТОП: с ценой источника {source_price_total} против {prev} раньше. Файлы не тронуты.")
        sys.exit(1)

    extra = [it["name"] for it in items.values() if it["_category"] == "Other"]
    if extra:
        print(f"Предметы только из калькулятора (категория Other): {len(extra)}, например: {extra[:15]}")

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(ALL_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated": stamp, "items": items}, f, ensure_ascii=False, indent=1)

    for fname, vkey, fields in VARIANTS:
        cats = {}
        for it in sorted(items.values(), key=lambda x: x["name"]):
            val = pick(it, fields)
            if val is None:
                continue
            bucket = cats.setdefault(it["_category"], {})
            if it["name"] in bucket:
                print(f"! дубль '{it['name']}' в {it['_category']}, берём первый")
                continue
            bucket[it["name"]] = val
        with open(fname, "w", encoding="utf-8") as f:
            json.dump({"variant_key": vkey, "updated": stamp, "categories": cats},
                      f, ensure_ascii=False, indent=1)
        print(f"{fname}: {sum(len(v) for v in cats.values())}")

    pet_items = [it for it in items.values() if it.get("_category") == "Pets"]
    explicit_base = sum(1 for it in pet_items if to_num(it.get("npRegularValue")) is not None)
    common_base = sum(
        1 for it in pet_items
        if to_num(it.get("npRegularValue")) is None
        and not any(to_num(it.get(k)) is not None for k in PET_VARIANT_SIBLINGS["npRegularValue"])
        and to_num(it.get("regularValue")) is not None
    )
    missing_base = sum(
        1 for it in items.values()
        if it.get("_category") == "Pets" and pick(it, VARIANTS[0][2]) is None
    )
    print(f"Base цены питомцев: {explicit_base} отдельных + {common_base} общих; без доступной цены: {missing_base}.")
    if missing_base:
        print(f"ВНИМАНИЕ: для {missing_base} питомцев нет Base-цены, и источник не позволяет безопасно её восстановить.")

    print("По категориям:", dict(by_cat))
    if short:
        print("GitHub: отправка пропущена, не все разделы собраны полностью.")
        sys.exit(1)

    try:
        publish_to_github(stamp, repo_root, branch)
    except RuntimeError as e:
        print(f"ОШИБКА публикации в GitHub: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
