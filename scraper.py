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

# Файл и variant_key (как в AMTradeCalc.lua). Для питомцев итоговая цена берётся
# прямо из карточки AMVGG после нажатия F/R/N/M: часть цен сайт вычисляет на клиенте,
# даже когда соответствующего поля нет в исходной записи.
VARIANTS = [
    ("values_base.json",    "Base",    ["npRegularValue"]),
    ("values_f.json",       "F",       ["fValue"]),
    ("values_r.json",       "R",       ["rValue"]),
    ("values_fr.json",      "FR",      ["regularValue"]),
    ("values_neon.json",    "Neon",    ["npNeonValue"]),
    ("values_neon_f.json",  "Neon|F",  ["nfValue"]),
    ("values_neon_r.json",  "Neon|R",  ["nrValue"]),
    ("values_neon_fr.json", "Neon|FR", ["neonValue"]),
    ("values_mega.json",    "Mega",    ["npMegaValue"]),
    ("values_mega_f.json",  "Mega|F",  ["mfValue"]),
    ("values_mega_r.json",  "Mega|R",  ["mrValue"]),
    ("values_mega_fr.json", "Mega|FR", ["megaValue"]),
]

PET_VARIANT_STATES = {
    "Base":     [False, False, False, False],
    "F":        [True,  False, False, False],
    "R":        [False, True,  False, False],
    "FR":       [True,  True,  False, False],
    "Neon":     [False, False, True,  False],
    "Neon|F":   [True,  False, True,  False],
    "Neon|R":   [False, True,  True,  False],
    "Neon|FR":  [True,  True,  True,  False],
    "Mega":     [False, False, False, True],
    "Mega|F":   [True,  False, False, True],
    "Mega|R":   [False, True,  False, True],
    "Mega|FR":  [True,  True,  False, True],
}

# Gray-code-like order reduces button changes. Repeat Base/FR only to move between
# the regular, Neon, and Mega groups while keeping each card's state predictable.
PET_VARIANT_SEQUENCE = [
    "FR", "F", "Base", "R", "FR",
    "Neon", "Neon|F", "Neon|FR", "Neon|R", "Neon", "Base",
    "Mega", "Mega|F", "Mega|FR", "Mega|R", "Mega",
]
PET_VARIANT_BY_FIELD = {
    fields[0]: variant_key
    for _, variant_key, fields in VARIANTS
}

ALL_FILE = "values_all.json"
DEPENDENCY_FILE = "requirements.txt"
GIT_REMOTE = "origin"
EXPECTED_GITHUB_REMOTE = "https://github.com/Andrewlol-solo/adoptme-values"
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
    repo_files = ["scraper.py", DEPENDENCY_FILE, *generated_files]
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


def scrape_pet_card_variants(expected_count):
    """Read all 12 displayed variants by toggling each pet card's F/R/N/M buttons."""
    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError as e:
        raise RuntimeError(
            "Для считывания кнопок F/R/N/M нужен Selenium. Установи зависимости: "
            "python -m pip install -r requirements.txt"
        ) from e

    if not expected_count:
        raise RuntimeError("Сайт не сообщил число питомцев; безопасно сверить карточки нельзя.")

    options = webdriver.ChromeOptions()
    options.page_load_strategy = "eager"
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-first-run")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=en-US")
    for base_dir in (
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LOCALAPPDATA"),
    ):
        if not base_dir:
            continue
        chrome = Path(base_dir) / "Google" / "Chrome" / "Application" / "chrome.exe"
        if chrome.is_file():
            options.binary_location = str(chrome)
            break

    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(90)
        driver.set_script_timeout(600)
        try:
            driver.get(BASE + "/values/pets")
        except TimeoutException:
            # Ads or analytics can keep loading after the public pet cards are ready.
            pass

        wait = WebDriverWait(driver, 90)
        wait.until(lambda d: d.execute_script("return document.readyState") != "loading")
        wait.until(lambda d: d.execute_script(
            "return document.querySelectorAll('main a[href^=\"/pet/\"]').length"
        ) > 0)
        count = driver.execute_script(
            "return document.querySelectorAll('main a[href^=\"/pet/\"]').length"
        )
        if count != expected_count:
            raise RuntimeError(
                f"На странице AMVGG найдено {count} карточек питомцев, "
                f"а раздел сайта сообщает {expected_count}. Файлы не будут заменены."
            )

        print(f"AMVGG: переключаю F/R/N/M у {count} карточек питомцев…")
        script = r"""
            const expectedCount = arguments[0];
            const sequence = arguments[1];
            const states = arguments[2];
            const finish = arguments[arguments.length - 1];
            const selector = 'main a[href^="/pet/"]';
            const labels = ['Toggle Fly', 'Toggle Ride', 'Toggle Neon', 'Toggle Mega'];
            const linksNow = () => Array.from(document.querySelectorAll(selector));
            const waitForPaint = () => new Promise(resolve =>
                requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const cardAt = index => linksNow()[index]?.parentElement;
            const stateOf = card => labels.map(label => {
                const button = card.querySelector(`button[aria-label="${label}"]`);
                if (!button) throw new Error(`Не найдена кнопка ${label}`);
                return button.classList.contains('text-white');
            });
            const valueOf = card => {
                const label = Array.from(card.querySelectorAll('span'))
                    .find(element => element.textContent.trim() === 'Value');
                const value = label?.nextElementSibling?.textContent.trim() || '';
                return /^[0-9]+(?:\.[0-9]+)?$/.test(value) ? value : null;
            };
            (async () => {
                try {
                    let links = linksNow();
                    if (links.length !== expectedCount) {
                        throw new Error(`Карточек в DOM: ${links.length}, ожидалось: ${expectedCount}`);
                    }
                    // Confirm React has attached handlers before trying to collect values.
                    const probe = links[0].parentElement.querySelector('button[aria-label="Toggle Fly"]');
                    if (!probe) throw new Error('У первой карточки нет кнопки Toggle Fly');
                    const initialProbeState = probe.classList.contains('text-white');
                    let interactive = false;
                    for (let attempt = 0; attempt < 20; attempt++) {
                        const currentProbe = linksNow()[0].parentElement.querySelector('button[aria-label="Toggle Fly"]');
                        currentProbe.click();
                        await waitForPaint();
                        const updatedProbe = linksNow()[0].parentElement.querySelector('button[aria-label="Toggle Fly"]');
                        if (updatedProbe.classList.contains('text-white') !== initialProbeState) {
                            interactive = true;
                            updatedProbe.click();
                            await waitForPaint();
                            break;
                        }
                        await new Promise(resolve => setTimeout(resolve, 250));
                    }
                    if (!interactive) throw new Error('Кнопки карточки не активировались после загрузки страницы');
                    links = linksNow();
                    const names = links.map((link, index) => {
                        const card = link.parentElement;
                        const heading = card?.querySelector('h2');
                        if (!heading) throw new Error(`У карточки ${index + 1} нет названия`);
                        return heading.textContent.trim();
                    });
                    const prices = {};
                    for (const key of sequence) {
                        const target = states[key];
                        if (!target) throw new Error(`Неизвестная комбинация ${key}`);
                        links = linksNow();
                        for (let index = 0; index < links.length; index++) {
                            const card = links[index].parentElement;
                            for (let buttonIndex = 0; buttonIndex < labels.length; buttonIndex++) {
                                const button = card.querySelector(`button[aria-label="${labels[buttonIndex]}"]`);
                                if (!button) throw new Error(`${names[index]}: не найдена кнопка ${labels[buttonIndex]}`);
                                if (button.classList.contains('text-white') !== target[buttonIndex]) {
                                    button.click();
                                }
                            }
                        }
                        let stateMismatch = null;
                        for (let attempt = 0; attempt < 20; attempt++) {
                            await waitForPaint();
                            links = linksNow();
                            stateMismatch = null;
                            for (let index = 0; index < links.length; index++) {
                                const actual = stateOf(links[index].parentElement);
                                if (actual.some((value, i) => value !== target[i])) {
                                    stateMismatch = {name: names[index], actual};
                                    break;
                                }
                            }
                            if (!stateMismatch) break;
                            await new Promise(resolve => setTimeout(resolve, 50));
                        }
                        if (stateMismatch) {
                            throw new Error(`${stateMismatch.name}: ${key} ожидал ${JSON.stringify(target)}, ` +
                                `получил ${JSON.stringify(stateMismatch.actual)}`);
                        }
                        links = linksNow();
                        const values = [];
                        for (let index = 0; index < links.length; index++) {
                            const card = links[index].parentElement;
                            const value = valueOf(card);
                            if (value === null) throw new Error(`${names[index]}: цена ${key} не отображается`);
                            values.push(value);
                        }
                        if (!(key in prices)) prices[key] = values;
                    }

                    // Leave this temporary browser in the page's familiar FR state.
                    links = linksNow();
                    const restore = states['FR'];
                    for (let index = 0; index < links.length; index++) {
                        const card = links[index].parentElement;
                        for (let buttonIndex = 0; buttonIndex < labels.length; buttonIndex++) {
                            const button = card.querySelector(`button[aria-label="${labels[buttonIndex]}"]`);
                            if (button && button.classList.contains('text-white') !== restore[buttonIndex]) button.click();
                        }
                    }
                    await waitForPaint();
                    finish({names, prices});
                } catch (error) {
                    finish({error: String(error), stack: error?.stack || ''});
                }
            })();
        """
        result = driver.execute_async_script(
            script, expected_count, PET_VARIANT_SEQUENCE, PET_VARIANT_STATES
        )
        if not isinstance(result, dict) or result.get("error"):
            detail = result.get("error", "неожиданный ответ браузера") if isinstance(result, dict) else repr(result)
            raise RuntimeError(f"Не удалось прочитать цены с карточек AMVGG: {detail}")
        names = result.get("names", [])
        if len(names) != expected_count:
            raise RuntimeError(f"Браузер вернул {len(names)} названий вместо {expected_count}.")
        values = result.get("prices", {})
        for _, variant_key, _ in VARIANTS:
            rows = values.get(variant_key)
            if not isinstance(rows, list) or len(rows) != expected_count:
                raise RuntimeError(f"Для варианта {variant_key} получено неполное число цен.")
            if any(to_num(value) is None for value in rows):
                raise RuntimeError(f"Для варианта {variant_key} сайт показал некорректную цену.")
        return names, values
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(
            "Не удалось открыть карточки AMVGG в headless Chrome. Проверь Chrome, "
            f"интернет и установленные зависимости. Подробности: {e}"
        ) from e
    finally:
        if driver is not None:
            driver.quit()


def apply_pet_card_variants(items, expected_count):
    pets = [item for item in items.values() if item.get("_category") == "Pets"]
    if len(pets) != expected_count:
        raise RuntimeError(
            f"В каталоге собрано {len(pets)} питомцев, сайт заявляет {expected_count}. "
            "Файлы не будут заменены."
        )

    by_name = {}
    duplicates = []
    for item in pets:
        if item["name"] in by_name:
            duplicates.append(item["name"])
        else:
            by_name[item["name"]] = item
    if duplicates:
        raise RuntimeError("Повторяются названия питомцев: " + ", ".join(duplicates[:10]))

    names, values = scrape_pet_card_variants(expected_count)
    if len(set(names)) != len(names):
        raise RuntimeError("В карточках AMVGG повторяются названия питомцев.")
    missing = sorted(set(by_name) - set(names))
    unexpected = sorted(set(names) - set(by_name))
    if missing or unexpected:
        raise RuntimeError(
            "Состав питомцев в карточках AMVGG не совпадает с данными раздела. "
            f"Нет карточек: {missing[:8]}; лишние карточки: {unexpected[:8]}."
        )

    for index, name in enumerate(names):
        by_name[name]["amvggCardVariants"] = {
            variant_key: to_num(values[variant_key][index])
            for _, variant_key, _ in VARIANTS
        }
    return len(pets)


def pick_base_pet(item):
    """Return the no-potion price displayed by the AMVGG card."""
    card_values = item.get("amvggCardVariants")
    if isinstance(card_values, dict) and "Base" in card_values:
        return to_num(card_values["Base"])
    return to_num(item.get("npRegularValue"))


def pick(item, fields):
    if item.get("_category") == "Pets":
        if not fields:
            return None
        variant_key = PET_VARIANT_BY_FIELD.get(fields[0])
        card_values = item.get("amvggCardVariants")
        if variant_key and isinstance(card_values, dict) and variant_key in card_values:
            return to_num(card_values[variant_key])
        # Preserve the exact source field only if browser collection was unavailable.
        # The normal run stops before writing when that collection fails.
        return to_num(item.get(fields[0]))
    for f in fields:
        v = to_num(item.get(f))
        if v is not None:
            return v
    return to_num(item.get("value"))


def verify_pet_variant_projection(items, variant_outputs):
    """Require every generated pet price to match the exact card-toggle result."""
    pets = [item for item in items.values() if item.get("_category") == "Pets"]
    for fname, variant_key, _ in VARIANTS:
        actual = variant_outputs[fname][1].get("Pets", {})
        missing = [item["name"] for item in pets if item["name"] not in actual]
        mismatched = [
            item["name"] for item in pets
            if item["name"] in actual
            and actual[item["name"]] != to_num(item.get("amvggCardVariants", {}).get(variant_key))
        ]
        if missing or mismatched:
            details = []
            if missing:
                details.append(f"нет цен у {len(missing)} питомцев: {missing[:5]}")
            if mismatched:
                details.append(f"не совпадают {len(mismatched)}: {mismatched[:5]}")
            raise RuntimeError(f"Проверка {fname} не прошла: " + "; ".join(details))
    return len(pets)


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
        print("СТОП: собрано меньше, чем заявлено по разделам:", ", ".join(short), ". Файлы не тронуты.")
        sys.exit(1)
    if failed_sections:
        print("СТОП: не удалось прочитать разделы", ", ".join(failed_sections), ". Файлы не публикуются.")
        sys.exit(1)
    if not items:
        print("СТОП: ничего не найдено, формат страницы мог измениться.")
        sys.exit(1)

    try:
        verified_pet_count = apply_pet_card_variants(items, expected.get("pets"))
    except RuntimeError as e:
        print(f"СТОП: {e} Файлы не тронуты.")
        sys.exit(1)

    prev = old_total()
    # Validate the full catalog count after reading the pet-card UI variants.
    source_price_total = len([1 for it in items.values() if pick(it, VARIANTS[3][2]) is not None])
    if prev and source_price_total < prev * MIN_RATIO:
        print(f"СТОП: с ценой источника {source_price_total} против {prev} раньше. Файлы не тронуты.")
        sys.exit(1)

    extra = [it["name"] for it in items.values() if it["_category"] == "Other"]
    if extra:
        print(f"Предметы только из калькулятора (категория Other): {len(extra)}, например: {extra[:15]}")

    # Build every output in memory first; verify every pet variant against the
    # value displayed by the corresponding AMVGG card before writing files.
    variant_outputs = {}
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
        variant_outputs[fname] = (vkey, cats)

    verified_pet_count = verify_pet_variant_projection(items, variant_outputs)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(ALL_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated": stamp, "items": items}, f, ensure_ascii=False, indent=1)

    for fname, _, _ in VARIANTS:
        vkey, cats = variant_outputs[fname]
        with open(fname, "w", encoding="utf-8") as f:
            json.dump({"variant_key": vkey, "updated": stamp, "categories": cats},
                      f, ensure_ascii=False, indent=1)
        print(f"{fname}: {sum(len(v) for v in cats.values())}")
    print(f"Проверка карточек: цены всех 12 вариантов сверены для {verified_pet_count} питомцев.")

    pet_items = [it for it in items.values() if it.get("_category") == "Pets"]
    explicit_base = sum(1 for it in pet_items if to_num(it.get("npRegularValue")) is not None)
    missing_base = sum(1 for it in pet_items if pick(it, VARIANTS[0][2]) is None)
    print(f"Base цены питомцев: {verified_pet_count - missing_base} считаны с карточек; "
          f"явное поле npRegularValue было у {explicit_base}.")
    if missing_base:
        print(f"ВНИМАНИЕ: {missing_base} карточек не показали Base-цену.")

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
