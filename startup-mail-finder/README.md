# scout — бесплатный поиск стартапов, первых лиц и публичных email

Ищет компании, достаёт опубликованные ими деловые email и имена первых лиц,
чтобы отправить резюме. Без ключей и подписок: `requests` + `bs4`
(Chromium — опционально, для SPA-сайтов).

## Почему не «парсинг Google Maps» и не LinkedIn

Обе идеи — скрапинг html-выдачи — нарушают ToS владельца выдачи: IP банится
за часы, а в случае LinkedIn банят ещё и личный аккаунт, из которого потом
не посмотреть вакансии. В 2022 году LinkedIn выиграл запрет на scraping в ЕС
(CJEU, дело hiQ); в США после hiQ v. LinkedIn это уже не преступление CFAA для
публичных страниц, но претензия по ToS остаётся. Плюс чтобы читать профили,
нужна залогиненная сессия, а значит — обход детекта, и инструмент превращается
в бан-генератор.

Рабочие бесплатные легальные пути — те, что реализованы здесь: официальные
API (OpenStreetMap, Wikidata, Y Combinator, Google Places) и данные,
опубликованные самими компаниями (Impressum, страницы команды, JSON-LD).

## Источники

| команда | что это | цена |
|---|---|---|
| `osm` | OpenStreetMap Overpass: компании по городу | 0, без ключа |
| `wikidata` | компании + основатели/CEO | 0, официальный SPARQL |
| `yc` | стартапы Y Combinator | 0, официальный API |
| `places` | Google Places Text Search | ~$200/мес free credit |
| `people` | первые лица + их адреса (обход сайтов) | 0 |

## Установка

```bash
pip3 install -r requirements.txt
sudo apt install chromium
```

## Использование

```bash
# 1. компании
python3 scout.py osm --city "Berlin" --limit 100 -o companies.csv
python3 scout.py wikidata --limit 50 --countries de,ee,il -o companies.csv
python3 scout.py yc --limit 200 -o companies.csv
python3 scout.py places --query "startup" --region DE -o companies.csv   # нужен ключ

# 2. склеить несколько источников по домену
python3 scout.py merge -i yc.csv osm.csv wikidata.csv -o companies.csv

# 3. деловые ящики компаний (--resume не обходит уже записанные домены)
python3 scout.py emails -i companies.csv -o leads.csv --workers 6 --js --resume

# 4. первые лица: имена + их адреса
python3 scout.py people -i companies.csv -o people.csv --workers 5 --resume
python3 scout.py people -i companies.csv -o people.csv --verify-smtp

# 5. черновики писем (отправляешь сам, из своего клиента)
python3 scout.py drafts -i leads.csv --lang en --name "Ivan Petrov" --email "ivan@example.com" \
    --role "Data Engineer" --why "I build ETL on Spark and want to sit close to the product."
python3 scout.py drafts --people people.csv --lang de --name "Ivan Petrov" --email "..." --role "Data Engineer"

# 6. весь пайплайн одной командой
python3 scout.py run --sources yc,osm --city Berlin --limit 40 --js \
    --name "Ivan Petrov" --email "ivan@example.com" --role "Data Engineer" --lang en

# 7. отправка через твой SMTP (сначала dry-run, потом --confirm)
export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USER=you@gmail.com
export SMTP_PASSWORD=app-password
export SMTP_FROM=you@gmail.com
python3 scout.py send --outdir outreach --name "Ivan Petrov" --limit 5
python3 scout.py send --outdir outreach --name "Ivan Petrov" --limit 5 --confirm --attach CV.pdf
```

`send` без `--confirm` ничего не отправляет. Уже посланные адреса пишутся в `outreach/sent.log` и пропускаются. Guess-адреса по умолчанию не шлются.

Выходные CSV и `outreach/` в git не коммитятся (см. `.gitignore`).

Google Places (если есть ключ):

```bash
export GOOGLE_PLACES_API_KEY=...
```

## Что в CSV

`leads.csv` (компании):

| колонка | смысл |
|---|---|
| `email` | лучший адрес (приоритет: свой домен > HR > hello/info > именной) |
| `email_kind` | `hr` / `role` / `named` — для `hr@` отклик надёжнее |
| `on_site` | `y`, если адрес на домене самой компании (признак качества) |
| `mx` | `ok`, если домен принимает почту |
| `emails` | все найденные через `;` |
| `js_rendered` | адрес, который дал headless-браузер |
| `note` | `no_public_email`, если адресов на сайте нет вообще |

`people.csv` (люди):

| колонка | смысл |
|---|---|
| `name` | имя первого лица |
| `role` | Geschäftsführer / Founder / CEO / … |
| `email_public` | адрес, опубликованный на сайте компании |
| `email_guesses` | адреса, построенные по корпоративному шаблону |
| `confidence` | `high` / `medium` / `guess` — см. ниже |
| `smtp` | `ok` / `catch-all` / `reject` / пусто — результат RCPT при `--verify-smtp` |

## Как ищутся люди (замена LinkedIn)

1. **Impressum.** В Германии, Австрии и Швейцарии закон обязывает в `Impressum`
   называть Geschäftsführer. Это самый чистый легальный источник имён первых лиц.
   Парсер режет блок по `HRB` / телефону / почте и валидирует, что выглядит как имя.
2. **JSON-LD** на сайте: `Organization` → `founder` / `employee`, иногда с почтой.
3. **`mailto:` со ссылкой**, подпись которой — имя человека.
4. **Wikidata** (P112 founder, P169 CEO) — когда сайт молчит.
5. **Построение адреса** по шаблону: `first.last@`, `first@`, `flast@`,
   `firstlast@` + транслитерация умляутов (`Gräbert` → `graebert`).

`confidence` честно разделяет эти источники:

- `high` — адрес опубликован на сайте самой компании;
- `medium` — опубликован, но на другом домене (обычно группа компаний);
- `guess` — построен по шаблону. С `--verify-smtp` проверяется через SMTP
  `RCPT TO` (письмо не отправляется). `ok` поднимает адрес, `reject` отбрасывает,
  `catch-all` не доказывает существование ящика. Без флага — не проверен.

## Как это устроено

- **Overpass**: `area[name=город][boundary=administrative]` + фильтр по тегам
  `office` и по имени (`tech|soft|web|data|...`). Обязателен `["website"]` —
  иначе выдача забита магазинами. `building~"^(office|commercial)$"` по большому
  городу даёт 504 — включать только для малых городов.
- **Wikidata**: `Q4830453` (любая фирма) + отраслевой фильтр `Q7397` (software)
  — без него прилетают InBev и Heineken. Страны задаются через `VALUES`:
  цепочка `?co wdt:P17 wd:Q183 . ?co wdt:P17 wd:Q191 .` требует, чтобы компания
  была во всех странах сразу, и всегда даёт пустой результат.
- **Обход сайта**: главная → ссылки с hint'ами
  `contact/about/careers/team/impressum` → типовые пути. Не больше 6 страниц
  и 12 URL в очереди.
- **Извлечение адресов**: три прохода — `mailto:`, видимый текст, сырой HTML
  (письма часто прячутся в JSON-LD, атрибутах и JS-бандлах).
  Плюс деобфускация `name (at) domain (dot) com`.
- **Приоритет адреса**: свой домен +40, freemail +15, чужой домен −25
  (обычно агентство или мусор из бандла), `hr@/jobs@/careers@` +100.
  Плейсхолдеры из шаблонов сайтов (`you@email.com`, `name@company.com`,
  `jane@acme.com`) отбрасываются.
- **Фолбэки реальности**: битый SSL (`verify=False`, пишется в stderr один раз
  на хост), несуществующий `www.`, мёртвый хост, 504 Overpass (повтор на том же
  зеркале, потом следующее), 0 совпадений (автоповтор с более широким фильтром).
- **Черновики**: `--lang en` (YC/US, по умолчанию) или `--lang de` (DACH).
  Свои тексты — `templates/en.txt` / `templates/de.txt` и `--template`.
- **Код**: `scout.py` — CLI, логика в `scoutlib/` (`sources`, `crawl`, `people`,
  `emails`, `drafts`).
- **Overpass не любит браузерный UA**: их Apache отвечает `406` на строку,
  мимикрирующую под Chrome. Для API отправляется честный бот-UA, для обхода
  сайтов — браузерный, иначе сайты банят.

`robots.txt` читается один раз на домен и кэшируется; по умолчанию соблюдается
(`--ignore-robots` есть, но им лучше не пользоваться).

## Ограничения

- **Покрытие OSM неравномерно.** Berlin/Tallinn дают десятки стартапов, а в
  Тарту — ноль (проверено: `office` + `website` там отсутствуют). В прогоне по
  Берлину и Таллину hit rate писем — **13 из 19 сайтов (68 %)**.
- Wikidata знает основателей далеко не у всех компаний. Для германских компаний
  Impressum полезнее Wikidata: из 15 берлинских сайтов имена нашлись у 2.
- Имена на `/team` публикуют далеко не все: в прогоне по Берлину из 15 компаний
  реальные люди нашлись только у ee-t.de и sprylab.com.
- У части компаний (ratiodata, Microsoft) публичного email нет вообще — форма
  отправки или виджет. Такие строки получают `note = no_public_email`.
- `pages_crawled=0` — сайт не открылся: сеть, DNS, мёртвый хост.
- Адреса с `confidence=guess` без `--verify-smtp` не проверяются на существование.
  MX бывает, а ящик давно мёртв. Catch-all MX принимает любой local-part —
  `smtp=catch-all` не значит, что ящик жив. Не гони трафик на мёртвые адреса.
- Сайты отдают контакты нестабильно: между прогонами разница бывает
  (`bewerbung@` то находится, то нет).

## Свой список вместо скрапинга

`scout.py emails -i любой.csv` работает с любым CSV, где есть колонка
`website`. Туда годится:

- экспорт из LinkedIn «Take a copy of your data» (официальный, о себе);
- Sales Navigator (платный официальный инструмент) — выгрузка компаний;
- список с конференции, из GitHub-организаций или из вакансий;
- свой CSV руками.

Это легально, уважает ToS и не тратит твой IP-репутацию.

## Этика и закон

- Только публичные деловые контакты, опубликованные компанией для обращений.
- Одно письмо на компанию, вручную прочитанное перед отправкой, с темой
  «отклик на вакансию» и без требования отписки.
- Письма отправляются тобой вручную из своего клиента — история не теряется,
  домен не улетает в блок за спам.
- В ЕС (GDPR) и в части штатов есть правила о холодной рассылке: уведоми
  получателя и дай простой отказ от дальнейших писем. Отклик на вакансию по
  публичному адресу — обычная практика, но массовая отправка без разбора
  адресатов — уже нет.
