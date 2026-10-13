import json
import os
import re
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
EXTRA_PAGES = ["/calculator"]   # предметы, которых нет в списках разделов

# файл, variant_key (как в AMTradeCalc.lua), поля сайта по приоритету
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
MIN_RATIO = 0.7
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


def github_target():
    try:
        root = Path(run_git(["rev-parse", "--show-toplevel"], SCRIPT_DIR).stdout.strip()).resolve()
    except RuntimeError as e:
        raise RuntimeError(
            "папка scraper.py не находится в Git-репозитории. Клонируй репозиторий GitHub "
            "и помести туда скрипт и файлы values_*.json."
        ) from e

    try:
        run_git(["remote", "get-url", "--push", GIT_REMOTE], root)
    except RuntimeError as e:
        raise RuntimeError(
            f"не настроен remote {GIT_REMOTE!r}. Добавь его командой "
            "git remote add origin <URL_твоего_GitHub-репозитория>."
        ) from e

    try:
        branch = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], root).stdout.strip()
    except RuntimeError as e:
        raise RuntimeError("Git находится в detached HEAD; переключись на ветку перед запуском.") from e

    if not branch:
        raise RuntimeError("не удалось определить текущую Git-ветку.")
    return root, branch


def publish_to_github(stamp, repo_root, branch):
    generated_files = [ALL_FILE, *(fname for fname, _, _ in VARIANTS)]
    try:
        paths = [
            (SCRIPT_DIR / filename).resolve().relative_to(repo_root).as_posix()
            for filename in generated_files
        ]
    except ValueError as e:
        raise RuntimeError("сгенерированные файлы находятся за пределами Git-репозитория.") from e

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
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pick(item, fields):
    for f in fields:
        v = to_num(item.get(f))
        if v is not None:
            return v
    # Non-pet sections expose a single generic value instead of pet variant fields.
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
    pages = [(s, p, c) for s, (p, c) in SECTIONS.items()] + [("extra", p, "Other") for p in EXTRA_PAGES]
    for section, path, category in pages:
        html = fetch(path)
        found = extract_items(html)
        m = EXPECT_RE.search(html)
        if m and section in SECTIONS:
            expected[section] = int(m.group(1))
        new = 0
        for it in found:
            if it["id"] not in items:
                it["_category"] = category
                items[it["id"]] = it
                new += 1
        print(f"[{section}] на странице {len(found)}, новых {new}"
              + (f", сайт заявляет {expected[section]}" if section in expected else ""))

    by_cat = Counter(i["_category"] for i in items.values())
    short = [s for s, n in expected.items() if by_cat.get(SECTIONS[s][1], 0) < n]
    if short:
        print("ВНИМАНИЕ, собрано меньше, чем заявлено:", ", ".join(short))
    if not items:
        print("СТОП: ничего не найдено, формат страницы мог измениться.")
        sys.exit(1)
    prev = old_total()
    base_total = len([1 for it in items.values() if pick(it, VARIANTS[0][2]) is not None])
    if prev and base_total < prev * MIN_RATIO:
        print(f"СТОП: с ценой {base_total} против {prev} раньше. Файлы не тронуты.")
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
