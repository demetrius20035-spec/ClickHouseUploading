-- ============================================================================
-- DDL-шаблоны схемы v2: ReplacingMergeTree, только INSERT, без мутаций на пачку.
-- Обоснование и порядок миграции: docs/audit/08-руководство-по-исправлению.md.
--
-- Именование (должно совпадать с модулем ВыгрузкаКликхаус.bsl):
--   документы:            mega.dokument_<имя>_v2
--   табличные части:      mega.dokument_<имя>_<тч>_v2   (и spravochnik_... аналогично)
--   справочники:          mega.spravochnik_<имя>_v2
--   регистры накопления:  mega.registr_nakopleniya_<имя>_v2
--
-- В каждую таблицу добавляются два служебных поля:
--   _version    UInt64 — версия строки (мс универсального времени начала прогона);
--   _is_deleted UInt8  — мягкое удаление (1 = надгробие).
--
-- Прочие колонки скопируйте из существующих таблиц mega.* (SHOW CREATE TABLE ...).
-- Модуль отправляет JSONEachRow; отсутствующие в JSON колонки заполняются значениями
-- по умолчанию (input_format_defaults_for_omitted_fields включён в CH по умолчанию) —
-- на этом построены строки-надгробия регистров.
--
-- ВНИМАНИЕ: вариант движка ReplacingMergeTree(_version, _is_deleted) (движок сам
-- отбрасывает удалённые при FINAL) требует ClickHouse >= 23.2. Ниже используется
-- универсальный ReplacingMergeTree(_version) + фильтр _is_deleted = 0 во view.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. ШАБЛОНЫ ТАБЛИЦ
-- ----------------------------------------------------------------------------

-- 1.1. Шапка документа/справочника: одна строка на ссылку.
--      Ключ сортировки — ссылка. Перевыгрузка объекта замещает строку по версии.
CREATE TABLE mega.dokument_postuplenie_na_raschetnyy_schet_v2
(
    ssylka      String,          -- навигационная ссылка
    data        DateTime,
    nomer       String,
    -- ... остальные реквизиты из текущей таблицы ...
    _version    UInt64,
    _is_deleted UInt8 DEFAULT 0
)
ENGINE = ReplacingMergeTree(_version)
ORDER BY ssylka;

-- 1.2. Табличная часть: строка на (ссылка, номер строки).
--      Живые строки определяются версией ШАПКИ (см. view 2.2) — это закрывает
--      фантомы при уменьшении числа строк ТЧ.
CREATE TABLE mega.dokument_postuplenie_na_raschetnyy_schet_tovary_v2
(
    ssylka        String,
    nomer_stroki  UInt32,
    -- ... реквизиты ТЧ ...
    _version      UInt64,
    _is_deleted   UInt8 DEFAULT 0
)
ENGINE = ReplacingMergeTree(_version)
ORDER BY (ssylka, nomer_stroki);

-- 1.3. Регистр накопления: строка на (регистратор, номер строки).
--      period НЕ входит в ключ сортировки: перепроведённая строка с изменившимся
--      периодом ЗАМЕЩАЕТ старую, а не живёт рядом с ней.
CREATE TABLE mega.registr_nakopleniya_prodazhi_v2
(
    registrator    String,
    period         DateTime,
    nomer_stroki   UInt32,
    vid_dvizheniya UInt8,        -- 0 = Приход, 1 = Расход
    -- ... измерения и ресурсы ...
    _version       UInt64,
    _is_deleted    UInt8 DEFAULT 0
)
ENGINE = ReplacingMergeTree(_version)
ORDER BY (registrator, nomer_stroki)
PARTITION BY toYYYYMM(period);


-- ----------------------------------------------------------------------------
-- 2. ПРЕДСТАВЛЕНИЯ ДЛЯ ЧТЕНИЯ (ловушка FINAL — поправка Ф-03)
--    FINAL схлопывает только одинаковые ключи сортировки; когда новая версия
--    содержит МЕНЬШЕ строк, чем старая, «лишние» старые строки остаются видимыми.
--    Поэтому ТЧ и регистры читаются по max-версии владельца.
-- ----------------------------------------------------------------------------

-- 2.1. Шапки: FINAL достаточно (одна строка на ключ)
CREATE VIEW mega.v_dokument_postuplenie AS
SELECT * FROM mega.dokument_postuplenie_na_raschetnyy_schet_v2 FINAL
WHERE _is_deleted = 0;

-- 2.2. ТЧ: живые строки — версии, которой является ШАПКА
CREATE VIEW mega.v_dokument_postuplenie_tovary AS
SELECT t.*
FROM mega.dokument_postuplenie_na_raschetnyy_schet_tovary_v2 AS t
INNER JOIN
(
    SELECT ssylka, max(_version) AS v
    FROM mega.dokument_postuplenie_na_raschetnyy_schet_v2
    GROUP BY ssylka
) AS last ON t.ssylka = last.ssylka AND t._version = last.v;

-- 2.3. Регистры: живые строки — максимальная версия данного регистратора.
--      Корректно, потому что модуль вставляет регистратор целиком в одном INSERT,
--      а распроведённому регистратору вставляет надгробие (_is_deleted = 1).
CREATE VIEW mega.v_registr_prodazhi AS
SELECT r.*
FROM mega.registr_nakopleniya_prodazhi_v2 AS r
INNER JOIN
(
    SELECT registrator, max(_version) AS v
    FROM mega.registr_nakopleniya_prodazhi_v2
    GROUP BY registrator
) AS last ON r.registrator = last.registrator AND r._version = last.v
WHERE r._is_deleted = 0;


-- ----------------------------------------------------------------------------
-- 3. ПЕРВИЧНОЕ НАПОЛНЕНИЕ ИЗ СТАРЫХ ТАБЛИЦ
--    Вариант надёжнее — полная перевыгрузка из 1С за всю историю (уйдут дыры
--    от старых сбоев DELETE→INSERT). Быстрый вариант — перенос как версия 0:
-- ----------------------------------------------------------------------------

INSERT INTO mega.registr_nakopleniya_prodazhi_v2
SELECT *, 0 AS _version, 0 AS _is_deleted
FROM mega.registr_nakopleniya_prodazhi;


-- ----------------------------------------------------------------------------
-- 4. ПЛАНОВАЯ ЗАЧИСТКА ФАНТОМОВ (единственная мутация в схеме)
--    Запускать редко (раз в сутки/неделю) в окно низкой нагрузки — из 1С это
--    делает экспортная функция КХ_ЗачиститьФантомыРегистра(ИмяТаблицы).
-- ----------------------------------------------------------------------------

-- Оконные функции в HAVING в ClickHouse запрещены (допустимы только в SELECT/ORDER BY),
-- поэтому «не максимальная версия регистратора» выражается через NOT IN по парам
-- (registrator, max(_version)). Подзапрос вычисляется один раз в момент постановки
-- мутации; куски, вставленные позже, не затрагиваются.
ALTER TABLE mega.registr_nakopleniya_prodazhi_v2
DELETE WHERE (registrator, _version) NOT IN
(
    SELECT registrator, max(_version)
    FROM mega.registr_nakopleniya_prodazhi_v2
    GROUP BY registrator
)
SETTINGS mutations_sync = 0;

-- Дополнительно: периодический OPTIMIZE схлопывает дубли одинаковых ключей
-- (замещённые версии), не трогая фантомы с разными ключами:
-- OPTIMIZE TABLE mega.registr_nakopleniya_prodazhi_v2 FINAL;


-- ----------------------------------------------------------------------------
-- 5. ПОРЯДОК МИГРАЦИИ БЕЗ ОСТАНОВКИ (кратко; подробно — в руководстве 08, п. 1.6)
--    1) создать *_v2-таблицы и view по шаблонам выше — для КАЖДОЙ выгружаемой
--       таблицы (список объектов — в модуле, раздел «Метаданные»);
--    2) первичное наполнение (п. 3) или полная перевыгрузка из 1С;
--    3) модуль уже собирает имена с суффиксом _v2 (функция КХ_СуффиксСхемы);
--    4) перевести отчёты/дэшборды на view v_*;
--    5) понаблюдать неделю, удалить старые таблицы.
-- ----------------------------------------------------------------------------
