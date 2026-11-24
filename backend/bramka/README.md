# bramka – wywołania modeli przez OpenRouter

Jedna funkcja `wywolaj` wysyła zapytanie do modelu przez OpenRouter i zwraca ujednolicony wynik (tekst, tokeny, koszt, błąd). Ustawienia modeli są w pliku JSON, a nie w kodzie. Paczka przeniesiona z innego projektu i dopasowana do serwera NanoLab (FastAPI).

## Co robi bramka

- **Profile w `modele.json`**: model, opcjonalny wysiłek myślenia (`low` / `medium` / `high`; brak = model bez myślenia, np. GPT-4o), `max_tokens`, ustawienia OpenRouter (np. tylko dany dostawca, bez zbierania danych), cennik. Zmiana modelu = zmiana nazwy profilu, bez ruszania kodu.
- **Adapter dostawcy**: budowa zapytania i odczyt odpowiedzi to dwie funkcje bez sieci (`openrouter_zapytanie`, `openrouter_odczyt`). Nowy dostawca = dwie nowe funkcje i wpis w `ADAPTERY`.
- **Odpowiedź w JSON według schematu**: gdy podano schemat, wymusza `response_format` typu `json_schema` (`strict`); bez schematu – zwykły tekst.
- **Obrazy**: lista ścieżek do plików albo bajtów (obraz już w pamięci; typ PNG/JPEG z nagłówka), wysyłane jako data URL (base64, `detail: high`) przed tekstem zlecenia.
- **Ponowienia**: błędy przejściowe (HTTP 429/502/503/504, przekroczony czas, zerwane połączenie) – 2 ponowienia po 3 s i 10 s (użytkownik czeka w przeglądarce). Gdy błąd nie ustąpi, w wyniku jest `blad.przejsciowy = True`. Limit czasu odpowiedzi: 180 s.
- **Rozpoznanie wyniku**: `powod` = `koniec` / `uciecie` (limit tokenów) / `odmowa` / `blad`, a w `blad.rodzaj` szczegół (np. `http`, `pusta_tresc`, `przerwane`).
- **Koszt**: dla każdej wysyłki większy z dwóch – z cennika profilu i z kwoty podanej przez OpenRouter. Suma wszystkich prób; `koszt_niepewny = True`, gdy wysyłka nie dostała odpowiedzi (dostawca mógł naliczyć).
- **Ctrl+C działa od razu** (na Windows zablokowane `requests.post` normalnie czeka do końca limitu czasu); co 15 s komunikat o czekaniu.
- **Bezpieczniki przed wysyłką**: brak klucza albo brak `MAX_OUTPUT_TOKENS` → nic nie idzie do API.
- **Bezpłatne dodatki OpenRouter**: `stan_klucza` (zużycie i limit konta), `pobierz_szczegoly` / `dopisz_szczegoly` (statystyki generacji, m.in. dokładne tokeny myślenia).
- **Log NDJSON** (`log_ndjson.py`): jedna linia pliku = jedno zdarzenie; klucze API i nazwa użytkownika w ścieżkach zamazane; bez treści promptów i odpowiedzi. Na stdout nic – postęp idzie na stderr.

## Pliki

| Plik | Zawartość |
|------|-----------|
| `bramka.py` | całe wywołanie API |
| `wywolanie_http.py` | NanoLab: wywołanie z serwera FastAPI (wynik → błąd HTTP) i listy modeli dla formularzy |
| `modele.json` | dostawcy i profile modeli – **ceny i nazwy modeli sprawdź przed użyciem** |
| `log_ndjson.py` | log zdarzeń NDJSON z zamazywaniem sekretów |
| `schematy/log.json` | schemat jednej linii logu; własne pola zdarzeń dopisz w `properties` |
| `test_bramka.py` | test bez sieci i bez kosztu (atrapa HTTP) |

## Użycie w NanoLab

Routery nie wołają `bramka.wywolaj` bezpośrednio, tylko `wywolanie_http.wywolaj_http`:

```python
from bramka import bramka, log_ndjson
from bramka.wywolanie_http import MODELE_JSON, modele_zadania, wywolaj_http

LOG = log_ndjson.Log("logi", "tem_analysis")     # jeden log na czas działania serwera
profil = bramka.wczytaj_profil(MODELE_JSON, "gpt61sol-orv", {"etap": "nanolab-tem"})   # {etap} = nazwa w panelu OpenRouter
wynik = wywolaj_http(profil, instrukcja, zlecenie, [jpg_bajty], schemat, LOG)                # domyślnie limit MAX_OUTPUT_TOKENS
wynik = wywolaj_http(profil, instrukcja, zlecenie, [], schemat, LOG, limit_env="MAX_OUTPUT_TOKENS_ARTICLE")
dane = json.loads(wynik["tekst"])
```

- `wywolaj_http` zwraca wynik bramki tylko przy `powod == "koniec"`; inaczej rzuca `HTTPException`: brak klucza / limitu → 500, ucięcie → 502 (z nazwą zmiennej limitu), błąd przejściowy po ponowieniach → 503, pozostałe → 502. Nie czeka na szczegóły generacji (`szczegoly=False`). Brak `OPENROUTER_API_KEY_VISION` → klucz `OPENROUTER_API_KEY`.
- **Listy modeli**: profil z polami `"zadanie"` (`pdf` / `tem`) i `"etykieta"` pojawia się w formularzu – `modele_zadania(zadanie)` → `[{id, label, profil}]`, endpoint `GET /api/llm/models/{zadanie}`. Profile bez `zadanie` (`gpt61sol-or-medium`, `sonnet55-or-high`) zostały z paczki źródłowej – używa ich test.
- **Limity tokenów wyjścia** w `.env`: `MAX_OUTPUT_TOKENS` (analiza TEM), `MAX_OUTPUT_TOKENS_ARTICLE` (import PDF). Profil może zejść tylko niżej.
- **Logi**: `backend/logi/tem_analysis.ndjson`, `backend/logi/llm_call.ndjson`; poprzedni plik przy starcie serwera idzie do `logi/archiwum/`.

## Instalacja i test

1. `pip install requests jsonschema`
2. W `backend/.env`: `OPENROUTER_API_KEY`, opcjonalnie `OPENROUTER_API_KEY_VISION`, `MAX_OUTPUT_TOKENS`, `MAX_OUTPUT_TOKENS_ARTICLE`.
3. `PYTHONIOENCODING=utf-8 python bramka/test_bramka.py` – na końcu ma wypisać `test_bramka: 48/48` (bez `PYTHONIOENCODING` konsola Windows nie wypisze strzałek i test przerwie się błędem kodowania).

Test sprawdza konkretne wartości profilu `gpt61sol-or-medium` (model, `max_tokens`, cennik) i przerwy ponowień `[3, 10]` – po zmianie profilu albo stałych popraw oczekiwane wartości w teście.

## Użycie poza serwerem (skrypty)

```python
env = bramka.wczytaj_env(".env")
profil = bramka.wczytaj_profil("modele.json", "sonnet55-or-high", {"etap": "odczyt"})
log = log_ndjson.Log("logi", "odczyt")  # logi/odczyt.ndjson, poprzedni przebieg idzie do logi/archiwum/
log.przechwyc_wyjatki()
log.start(profil=profil["nazwa"])
if bramka.stan_klucza(profil, env, log) is None:  # bezpłatne sprawdzenie klucza przed płatnym zapytaniem
    raise SystemExit("klucz niedostępny")
wynik = bramka.wywolaj(profil, "Jesteś ...", "Opisz obraz.", ["obraz.png"], schemat, env, log)
log.koniec("ok" if wynik["ok"] else "blad", szczegoly={"koszt_usd": wynik["koszt_usd"]})
```

Najważniejsze pola wyniku: `ok`, `powod`, `tekst`, `blad`, `tokeny_wejscie`, `tokeny_wyjscie`, `tokeny_myslenie`, `tokeny_z_pamieci`, `koszt_usd`, `koszt_dostawcy_usd`, `koszt_niepewny`, `ponowienia`, `czas_ms`, `wywolanie_id`, `surowa` (ostatnia odpowiedź bez zmian).

Gdy najpierw chcesz zapisać odpowiedź, a dopiero potem czekać na statystyki (do 30 s): `wywolaj(..., szczegoly=False)`, zapis, potem `bramka.dopisz_szczegoly(profil, wynik, env, log)`.
