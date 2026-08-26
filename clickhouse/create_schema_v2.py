#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
create_schema_v2.py — массовое приведение таблиц ClickHouse к схеме v2
(ReplacingMergeTree, только INSERT) из docs/audit/08-руководство-по-исправлению.md.

Что делает:
  1. Находит в базе (по умолчанию `mega`) все таблицы с префиксами
     dokument_ / spravochnik_ / registr_nakopleniya_ (кроме уже *_v2 и view).
  2. Классифицирует каждую: шапка (есть ssylka), табличная часть
     (ssylka + nomer_stroki), регистр накопления (префикс registr_nakopleniya_).
  3. Для каждой создаёт `<имя>_v2` с теми же колонками (типы берутся из
     system.columns — вручную ничего перечислять не нужно) плюс служебные
     `_version UInt64` и `_is_deleted UInt8 DEFAULT 0`:
        шапка:   ENGINE ReplacingMergeTree(_version) ORDER BY ssylka
        ТЧ:      ... ORDER BY (ssylka, nomer_stroki)
        регистр: ... ORDER BY (registrator, nomer_stroki)
                 PARTITION BY toYYYYMM(period)  (если period есть и это дата)
     Nullable у ключевых колонок снимается: ClickHouse не допускает Nullable
     в ORDER BY/PARTITION BY, а модуль эти поля всегда заполняет.
  4. Создаёт представления для чтения (ловушка FINAL, поправка Ф-03):
        v_<шапка>:   SELECT * FROM ..._v2 FINAL WHERE _is_deleted = 0
        v_<ТЧ>:      строки версии, которой является ШАПКА (join по max(_version) шапки)
        v_<регистр>: строки максимальной версии своего регистратора, _is_deleted = 0
  5. Миграции данных НЕТ — данные перезаливаются модулем с нуля.

Транслитерация имён: функция kh_transliterate() — точный порт
КХ_ТранслитерироватьСтроку() из ВыгрузкаКликхаус.bsl (сама BSL-функция не
меняется). Скрипту она нужна только для режима --check (сверка ожидаемых имён
по списку объектов 1С); имена создаваемых таблиц берутся из существующих —
то есть уже прошли через ту же транслитерацию на стороне 1С.

Примеры:
    python3 create_schema_v2.py --selftest
    python3 create_schema_v2.py --dry-run                # показать SQL, ничего не выполнять
    python3 create_schema_v2.py                          # создать *_v2 и view (IF NOT EXISTS)
    python3 create_schema_v2.py --recreate               # ПЕРЕСОЗДАТЬ *_v2 (DROP + CREATE)
    python3 create_schema_v2.py --tables dokument_prodazhi,registr_nakopleniya_prodazhi
    python3 create_schema_v2.py --check objects.txt      # сверить имена по списку объектов 1С

Подключение: --host/--port/--user/--password или переменные окружения
CH_HOST/CH_PORT/CH_USER/CH_PASSWORD (по умолчанию — значения из модуля).
Требуется только стандартная библиотека Python 3.7+.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Транслитерация — точный порт КХ_ТранслитерироватьСтроку() (BSL не меняем!)
# ---------------------------------------------------------------------------

TRANSLIT_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def kh_transliterate(text):
    """Повторяет КХ_ТранслитерироватьСтроку() шаг в шаг."""
    # 1. Транслитерация кириллицы
    parts = []
    for ch in text:
        low = ch.lower()
        if low in TRANSLIT_MAP:
            val = TRANSLIT_MAP[low]
            if low != ch and len(val) > 0:
                # первая буква заглавная, остальные строчные
                val = val[0].upper() + val[1:].lower()
            parts.append(val)
        else:
            code = ord(ch)
            if 65 <= code <= 90 or 97 <= code <= 122 or 48 <= code <= 57 or ch == "_":
                parts.append(ch)
            else:
                parts.append("_")
    result = "".join(parts)

    # 2. CamelCase -> snake_case (та же пара правил, что в BSL)
    snake = []
    length = len(result)
    for i, cur in enumerate(result):
        if i == 0:
            snake.append(cur)
            continue
        prev = result[i - 1]
        nxt_code = ord(result[i + 1]) if i < length - 1 else 0
        cur_code = ord(cur)
        prev_code = ord(prev)
        cur_upper = 65 <= cur_code <= 90
        prev_upper = 65 <= prev_code <= 90
        nxt_lower = 97 <= nxt_code <= 122
        prev_lower = 97 <= prev_code <= 122
        prev_digit = 48 <= prev_code <= 57
        if (prev_lower or prev_digit) and cur_upper:
            snake.append("_")
            snake.append(cur)
        elif prev_upper and cur_upper and nxt_lower:
            snake.append("_")
            snake.append(cur)
        else:
            snake.append(cur)
    s = "".join(snake)

    # 3. Нижний регистр
    s = s.lower()

    # 4. Схлопывание повторных подчёркиваний
    while "__" in s:
        s = s.replace("__", "_")

    # 5. Ровно по одному ведущему/замыкающему подчёркиванию (как в BSL)
    if s.startswith("_"):
        s = s[1:]
    if s.endswith("_"):
        s = s[:-1]

    # 6. Пустая строка
    if not s:
        s = "field"

    # 7. Начинается с цифры
    if "0" <= s[0] <= "9":
        s = "n_" + s

    return s


SELFTEST_CASES = [
    ("ПоступлениеНаРасчетныйСчет", "postuplenie_na_raschetnyy_schet"),
    ("СписаниеСРасчетногоСчета", "spisanie_s_raschetnogo_scheta"),
    ("IIKO_МРР", "iiko_mrr"),
    ("МРР", "mrr"),
    ("СтавкиНДС", "stavki_nds"),
    ("ДенежныеСредстваВПути", "denezhnye_sredstva_v_puti"),
    ("Ссылка", "ssylka"),
    ("НомерСтроки", "nomer_stroki"),
    ("Регистратор", "registrator"),
    ("ВидДвижения", "vid_dvizheniya"),
    ("Период", "period"),
    ("123Тест", "n_123_test"),
    ("  ", "field"),
    ("ЁжикВТумане", "yozhik_v_tumane"),
]

# ---------------------------------------------------------------------------
# HTTP-клиент ClickHouse (стандартная библиотека, как и в модуле — HTTP-порт)
# ---------------------------------------------------------------------------


class ClickHouseError(RuntimeError):
    pass


class ClickHouse:
    def __init__(self, host, port, user, password, secure=False, timeout=300):
        scheme = "https" if secure else "http"
        self.url = "%s://%s:%s/" % (scheme, host, port)
        self.headers = {
            "X-ClickHouse-User": user,
            "X-ClickHouse-Key": password,
            "Content-Type": "text/plain; charset=UTF-8",
        }
        self.timeout = timeout

    def execute(self, query):
        req = urllib.request.Request(
            self.url, data=query.encode("utf-8"), headers=self.headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            raise ClickHouseError("HTTP %s: %s" % (e.code, body.strip()[:2000]))
        except urllib.error.URLError as e:
            raise ClickHouseError("Нет соединения с ClickHouse: %s" % e.reason)

    def select_json(self, query):
        text = self.execute(query.rstrip().rstrip(";") + " FORMAT JSONEachRow")
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def sql_string(value):
    """Строковый литерал для SQL."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def ident(name):
    """Идентификатор в обратных кавычках."""
    return "`" + name.replace("`", "\\`") + "`"

# ---------------------------------------------------------------------------
# Построение DDL
# ---------------------------------------------------------------------------

HEADER, TCH, REGISTER = "шапка", "ТЧ", "регистр"

SERVICE_COLUMNS = ("_version", "_is_deleted")


def strip_nullable(ch_type):
    m = re.fullmatch(r"Nullable\((.*)\)", ch_type)
    if m:
        return m.group(1)
    m = re.fullmatch(r"LowCardinality\(Nullable\((.*)\)\)", ch_type)
    if m:
        return "LowCardinality(%s)" % m.group(1)
    return ch_type


def base_type(ch_type):
    return strip_nullable(ch_type)


def is_date_type(ch_type):
    t = base_type(ch_type)
    return t in ("Date", "Date32", "DateTime") or t.startswith("DateTime64")


def classify(name, columns, prefixes):
    names = {c["name"] for c in columns}
    if name.startswith(prefixes["register"]):
        if "registrator" in names and "nomer_stroki" in names:
            return REGISTER, None
        return None, "нет колонок registrator/nomer_stroki — пропуск"
    if "ssylka" in names and "nomer_stroki" in names:
        return TCH, None
    if "ssylka" in names:
        return HEADER, None
    return None, "нет колонки ssylka — пропуск"


def build_create_table(db, name, columns, kind, suffix):
    """CREATE TABLE ..._v2 по колонкам исходной таблицы."""
    if kind == HEADER:
        key_cols = ["ssylka"]
    elif kind == TCH:
        key_cols = ["ssylka", "nomer_stroki"]
    else:
        key_cols = ["registrator", "nomer_stroki"]

    partition_expr = None
    if kind == REGISTER:
        for c in columns:
            if c["name"] == "period" and is_date_type(c["type"]):
                partition_expr = "toYYYYMM(period)"
                break

    lines = []
    for c in columns:
        if c["name"] in SERVICE_COLUMNS:
            continue  # уже есть в источнике — добавим свои ниже
        col_type = c["type"]
        # Nullable недопустим в ключе сортировки и ключе партиционирования;
        # модуль эти поля всегда заполняет, поэтому снимаем Nullable безопасно
        if c["name"] in key_cols or (partition_expr and c["name"] == "period"):
            col_type = strip_nullable(col_type)
        line = "    %s %s" % (ident(c["name"]), col_type)
        kind_default = c.get("default_kind") or ""
        expr_default = c.get("default_expression") or ""
        if kind_default and expr_default:
            line += " %s %s" % (kind_default, expr_default)
        lines.append(line)

    lines.append("    `_version` UInt64")
    lines.append("    `_is_deleted` UInt8 DEFAULT 0")

    ddl = "CREATE TABLE IF NOT EXISTS %s.%s\n(\n%s\n)\nENGINE = ReplacingMergeTree(_version)\nORDER BY (%s)" % (
        ident(db),
        ident(name + suffix),
        ",\n".join(lines),
        ", ".join(ident(k) for k in key_cols),
    )
    if partition_expr:
        ddl += "\nPARTITION BY %s" % partition_expr
    return ddl


def build_view(db, name, kind, suffix, header_name=None):
    """CREATE VIEW v_<имя> по схеме из руководства 08 (поправка Ф-03)."""
    view = ident("v_" + name)
    table = "%s.%s" % (ident(db), ident(name + suffix))
    if kind == HEADER:
        body = "SELECT * FROM %s FINAL WHERE _is_deleted = 0" % table
    elif kind == TCH:
        header = "%s.%s" % (ident(db), ident(header_name + suffix))
        body = (
            "SELECT t.* FROM %s AS t INNER JOIN "
            "(SELECT ssylka, max(_version) AS v FROM %s GROUP BY ssylka) AS last "
            "ON t.ssylka = last.ssylka AND t._version = last.v" % (table, header)
        )
    else:
        body = (
            "SELECT r.* FROM %s AS r INNER JOIN "
            "(SELECT registrator, max(_version) AS v FROM %s GROUP BY registrator) AS last "
            "ON r.registrator = last.registrator AND r._version = last.v "
            "WHERE r._is_deleted = 0" % (table, table)
        )
    return "CREATE VIEW %s.%s AS %s" % (ident(db), view, body)

# ---------------------------------------------------------------------------
# Обход базы
# ---------------------------------------------------------------------------


def discover_tables(ch, db, prefixes, suffix, only=None):
    rows = ch.select_json(
        "SELECT name, engine FROM system.tables WHERE database = %s ORDER BY name"
        % sql_string(db)
    )
    skip_engines = {"View", "MaterializedView", "LiveView", "Dictionary"}
    result = []
    for r in rows:
        name = r["name"]
        if r.get("engine") in skip_engines:
            continue
        if name.startswith(".") or name.startswith("v_") or name.endswith(suffix):
            continue
        if not name.startswith(tuple(prefixes.values())):
            continue
        if only and name not in only:
            continue
        result.append(name)
    return result


def load_columns(ch, db, table):
    return ch.select_json(
        "SELECT name, type, default_kind, default_expression "
        "FROM system.columns WHERE database = %s AND table = %s ORDER BY position"
        % (sql_string(db), sql_string(table))
    )


def find_header(tch_name, header_names):
    """ТЧ dokument_x_tovary -> шапка dokument_x: самый длинный префикс."""
    best = None
    for h in header_names:
        if tch_name.startswith(h + "_") and (best is None or len(h) > len(best)):
            best = h
    return best

# ---------------------------------------------------------------------------
# Режимы
# ---------------------------------------------------------------------------


def run_selftest():
    failed = 0
    for src, expected in SELFTEST_CASES:
        got = kh_transliterate(src)
        status = "OK " if got == expected else "FAIL"
        if got != expected:
            failed += 1
        print("[%s] %-32r -> %-35r (ожидалось %r)" % (status, src, got, expected))

    # дымовой тест генерации DDL
    cols = [
        {"name": "registrator", "type": "Nullable(String)"},
        {"name": "period", "type": "Nullable(DateTime)"},
        {"name": "nomer_stroki", "type": "Nullable(UInt32)"},
        {"name": "summa", "type": "Nullable(Float64)"},
    ]
    ddl = build_create_table("mega", "registr_nakopleniya_prodazhi", cols, REGISTER, "_v2")
    checks = [
        "ReplacingMergeTree(_version)",
        "ORDER BY (`registrator`, `nomer_stroki`)",
        "PARTITION BY toYYYYMM(period)",
        "`registrator` String",          # Nullable снят с ключа
        "`period` DateTime",             # Nullable снят с ключа партиции
        "`summa` Nullable(Float64)",     # не-ключевые типы не тронуты
        "`_is_deleted` UInt8 DEFAULT 0",
    ]
    for c in checks:
        ok = c in ddl
        if not ok:
            failed += 1
        print("[%s] DDL содержит: %s" % ("OK " if ok else "FAIL", c))

    view = build_view("mega", "dokument_x_tovary", TCH, "_v2", "dokument_x")
    ok = "max(_version)" in view and "dokument_x_v2" in view
    if not ok:
        failed += 1
    print("[%s] view ТЧ читается по max-версии шапки" % ("OK " if ok else "FAIL"))

    print("\nСамотест: %s" % ("ПРОВАЛЕН (%d)" % failed if failed else "успешно"))
    return 1 if failed else 0


def run_check(path, prefixes, suffix):
    """Сверка ожидаемых имён таблиц по списку объектов 1С (строки вида
    Документ.ПоступлениеТоваровУслуг[.ИмяТЧ], Справочник.Банки, РегистрНакопления.Продажи)."""
    type_map = {
        "документ": prefixes["document"],
        "справочник": prefixes["catalog"],
        "регистрнакопления": prefixes["register"],
    }
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(".")
            prefix = type_map.get(parts[0].lower().replace(" ", ""))
            if prefix is None or len(parts) < 2:
                print("[??] %s — не понял тип объекта" % line)
                continue
            name = prefix + "_".join(kh_transliterate(p) for p in parts[1:])
            print("%s -> %s%s (view: v_%s)" % (line, name, suffix, name))
    return 0

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(
        description="Создание схемы v2 (ReplacingMergeTree) по существующим таблицам ClickHouse",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--host", default=os.environ.get("CH_HOST", "5.182.6.79"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("CH_PORT", "58123")))
    ap.add_argument("--user", default=os.environ.get("CH_USER", "mega_user"))
    ap.add_argument("--password", default=os.environ.get("CH_PASSWORD", "b%250iLy8hsOf9"))
    ap.add_argument("--secure", action="store_true", help="подключаться по https")
    ap.add_argument("--database", default="mega")
    ap.add_argument("--suffix", default="_v2")
    ap.add_argument("--tables", default="",
                    help="обработать только эти исходные таблицы (через запятую)")
    ap.add_argument("--dry-run", action="store_true",
                    help="напечатать SQL и выйти, ничего не выполняя")
    ap.add_argument("--recreate", action="store_true",
                    help="СНАЧАЛА DROP TABLE *_v2 (данные v2 будут потеряны; "
                         "исходные таблицы не трогаются)")
    ap.add_argument("--skip-views", action="store_true", help="не создавать view")
    ap.add_argument("--selftest", action="store_true",
                    help="самотест транслитерации и генерации DDL (без подключения)")
    ap.add_argument("--check", metavar="FILE",
                    help="сверить ожидаемые имена таблиц по списку объектов 1С (без подключения)")
    args = ap.parse_args()

    prefixes = {
        "document": "dokument_",
        "catalog": "spravochnik_",
        "register": "registr_nakopleniya_",
    }

    if args.selftest:
        return run_selftest()
    if args.check:
        return run_check(args.check, prefixes, args.suffix)

    only = {t.strip() for t in args.tables.split(",") if t.strip()} or None
    ch = ClickHouse(args.host, args.port, args.user, args.password, args.secure)

    version = ch.execute("SELECT version()").strip()
    print("ClickHouse %s на %s:%s, база %s" % (version, args.host, args.port, args.database))

    tables = discover_tables(ch, args.database, prefixes, args.suffix, only)
    if not tables:
        print("Не найдено ни одной исходной таблицы — проверьте базу и префиксы.")
        return 1
    print("Найдено исходных таблиц: %d\n" % len(tables))

    # Классификация и сбор плана
    infos = []      # (имя, вид, колонки)
    headers = []    # имена шапок — для привязки ТЧ
    skipped = []
    for name in tables:
        columns = load_columns(ch, args.database, name)
        kind, reason = classify(name, columns, prefixes)
        if kind is None:
            skipped.append((name, reason))
            continue
        infos.append((name, kind, columns))
        if kind == HEADER:
            headers.append(name)

    statements = []  # (метка, sql)
    for name, kind, columns in infos:
        if args.recreate:
            statements.append(
                ("DROP  %s%s" % (name, args.suffix),
                 "DROP TABLE IF EXISTS %s.%s"
                 % (ident(args.database), ident(name + args.suffix)))
            )
        statements.append(
            ("TABLE %s%s [%s]" % (name, args.suffix, kind),
             build_create_table(args.database, name, columns, kind, args.suffix))
        )

    if not args.skip_views:
        for name, kind, _ in infos:
            header_name = None
            if kind == TCH:
                header_name = find_header(name, headers)
                if header_name is None:
                    skipped.append((name, "не нашёл шапку для ТЧ — view пропущен"))
                    continue
            statements.append(
                ("DROP  v_%s" % name,
                 "DROP VIEW IF EXISTS %s.%s" % (ident(args.database), ident("v_" + name)))
            )
            statements.append(
                ("VIEW  v_%s [%s]" % (name, kind),
                 build_view(args.database, name, kind, args.suffix, header_name))
            )

    if args.dry_run:
        for _, sql in statements:
            print(sql + ";\n")
        for name, reason in skipped:
            print("-- ПРОПУЩЕНО %s: %s" % (name, reason))
        print("-- Всего инструкций: %d (dry-run, ничего не выполнено)" % len(statements))
        return 0

    errors = 0
    for label, sql in statements:
        try:
            ch.execute(sql)
            print("[OK]     %s" % label)
        except ClickHouseError as e:
            errors += 1
            print("[ОШИБКА] %s\n         %s" % (label, e))

    for name, reason in skipped:
        print("[ПРОПУСК] %s: %s" % (name, reason))

    print("\nГотово: %d инструкций, ошибок: %d, пропущено: %d"
          % (len(statements), errors, len(skipped)))
    if not errors:
        print("Дальше: перевести отчёты на view v_*, запустить полную перевыгрузку из 1С "
              "(модуль уже пишет в *%s)." % args.suffix)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
