# Bramka API: jedno miejsce wywołania modeli – wywolaj(profil, instrukcja, zlecenie, obrazy, schemat) zwraca ujednolicony wynik
# niezależnie od dostawcy. Adapter dostawcy = budowa zapytania i odczyt odpowiedzi (funkcje bez sieci); na razie tylko OpenRouter.
# NanoLab: obrazy jako ścieżki albo bajty (obraz już w pamięci), schemat i wysiłek myślenia opcjonalne, czasy dobrane do czekania w UI.
# Błąd przejściowy (HTTP 429/502/503/504, przekroczony czas, zerwane połączenie) ponawia bramka – 2 razy, po 3 s i 10 s; gdy nie ustąpi,
# wynik ma blad.przejsciowy (wywołujący powinien zatrzymać pracę – awaria sieci). Odpowiedź niezgodną z oczekiwaniami ponawia wywołujący.
# Koszt każdej wysyłki = większy z cennika profilu i kwoty dostawcy (suma wszystkich prób), kwota dostawcy obok do kontroli. Szczegóły generacji
# (bezpłatne statystyki pośrednika) – w wywolaj albo osobno dopisz_szczegoly, gdy wywołujący najpierw zapisuje odpowiedź.
# Nic na stdout: zdarzenia do logu NDJSON (log_ndjson.py), postęp na stderr.
import base64, json, mimetypes, os, pathlib, threading, time
import requests, urllib3

OPOZNIENIA_S = (3, 10)  # przerwy przed ponowieniami błędu przejściowego – użytkownik czeka w przeglądarce
KODY_PRZEJSCIOWE = {429, 502, 503, 504}  # przeciążenie, limit zapytań, błąd bramy – warto ponowić
LIMIT_CZASU_S = 180  # analiza TEM: ok. 2000 tokenów przy 30–60 tokenach/s ≈ 70 s, z zapasem
CO_ILE_POSTEP_S = 15
SZCZEGOLY_ODCZYTY, SZCZEGOLY_ODSTEP_S = 10, 3  # statystyki generacji do 30 s po odpowiedzi (zwykle dostępne po ok. 8 s)
WYSILKI = ("low", "medium", "high")


def wczytaj_env(sciezka):
    """Zmienne z pliku .env (KLUCZ=wartość); zmienne środowiska mają pierwszeństwo."""
    wartosci = {}
    if sciezka and pathlib.Path(sciezka).exists():
        for linia in pathlib.Path(sciezka).read_text(encoding="utf-8").splitlines():
            linia = linia.strip()
            if linia and not linia.startswith("#") and "=" in linia:
                k, v = linia.split("=", 1)
                wartosci[k.strip()] = v.strip().strip('"').strip("'")
    wartosci.update(os.environ)
    return wartosci


def wczytaj_profil(plik_modeli, nazwa, zmienne):
    """Profil z modele.json połączony z danymi dostawcy; wzory {nazwa} w nagłówkach wypełnione ze zmiennych (np. etap)."""
    konfiguracja = json.loads(pathlib.Path(plik_modeli).read_text(encoding="utf-8"))
    profil = konfiguracja["profile"][nazwa]
    if profil.get("wysilek") not in (None, *WYSILKI):  # brak = model bez myślenia (np. GPT-4o)
        raise ValueError(f"profil {nazwa}: wysilek {profil['wysilek']!r} spoza {WYSILKI}")
    dostawca = dict(konfiguracja["dostawcy"][profil["dostawca"]])
    if dostawca["adapter"] not in ADAPTERY:
        raise ValueError(f"dostawca {profil['dostawca']}: nieznany adapter {dostawca['adapter']!r}")
    dostawca["naglowki"] = {k: v.format(**zmienne) for k, v in dostawca.get("naglowki", {}).items()}
    return {"nazwa": nazwa, **profil, "dostawca_dane": dostawca}


# --- Adapter OpenRouter (API zgodne z OpenAI chat/completions) ---

def _obraz(obraz):
    """Obraz jako data URL w pełnej rozdzielczości (detail high). Ścieżka – typ z rozszerzenia; bajty – PNG albo JPEG z nagłówka."""
    if isinstance(obraz, bytes):
        typ, dane = ("image/png" if obraz.startswith(b"\x89PNG") else "image/jpeg"), obraz
    else:
        typ, dane = mimetypes.guess_type(str(obraz))[0] or "application/octet-stream", pathlib.Path(obraz).read_bytes()
    return {"type": "image_url", "image_url": {"url": f"data:{typ};base64," + base64.b64encode(dane).decode(), "detail": "high"}}


def openrouter_zapytanie(profil, instrukcja, zlecenie, obrazy, schemat, max_tokens):
    """Treść zapytania: instrukcja jako wiadomość systemowa, obrazy przed tekstem zlecenia, parametry_dostawcy bez zmian.
    Kształt JSON (response_format) tylko przy podanym schemacie; reasoning.effort tylko, gdy profil ma wysiłek."""
    tresc = {"model": profil["model"],
             "messages": [{"role": "system", "content": instrukcja},
                          {"role": "user", "content": [*(_obraz(o) for o in obrazy), {"type": "text", "text": zlecenie}]}],
             "max_tokens": max_tokens, **profil.get("parametry_dostawcy", {})}
    if schemat is not None:
        tresc["response_format"] = {"type": "json_schema", "json_schema": {"name": "wynik", "strict": True, "schema": schemat}}
    if profil.get("wysilek"):
        tresc["reasoning"] = {"effort": profil["wysilek"]}
    return tresc


def openrouter_odczyt(http, surowa):
    """Odpowiedź dostawcy → pola ujednoliconego wyniku: powod (koniec / uciecie / odmowa / blad), tekst, blad, tokeny, kwota dostawcy.
    Błąd rozpoznawany po kodzie HTTP, błędzie w treści przy HTTP 200, błędzie w wybranej odpowiedzi, ucięciu, odmowie, braku treści."""
    try:
        dane = json.loads(surowa)
    except ValueError:
        dane = None
    if not isinstance(dane, dict):
        dane = None
    usage = (dane or {}).get("usage") or {}
    wynik = {"tekst": None, "blad": None, "wywolanie_id": (dane or {}).get("id"),
             "dostawca_faktyczny": (dane or {}).get("provider"), "usage": usage,
             "tokeny_wejscie": usage.get("prompt_tokens"), "tokeny_wyjscie": usage.get("completion_tokens"),
             "tokeny_myslenie": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
             "tokeny_z_pamieci": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
             "koszt_dostawcy_usd": usage.get("cost")}
    if dane is None:
        return {**wynik, "powod": "blad", "blad": {"rodzaj": "odpowiedz_nie_json", "http": http}}
    if http != 200 or "error" in dane:
        e = dane.get("error") or {}
        return {**wynik, "powod": "blad", "blad": {"rodzaj": "http" if http != 200 else "blad_w_tresci_http200", "http": http,
                                                   "kod": e.get("code"), "komunikat": e.get("message"), "metadata": e.get("metadata")}}
    wybor = (dane.get("choices") or [{}])[0]
    wiadomosc = wybor.get("message") or {}
    powod = wybor.get("finish_reason")
    zakonczenie = {"http": 200, "finish_reason": powod, "native_finish_reason": wybor.get("native_finish_reason")}
    if "error" in wybor or powod == "error":
        e = wybor.get("error") or {}
        return {**wynik, "powod": "blad", "blad": {"rodzaj": "blad_w_odpowiedzi", **zakonczenie, "kod": e.get("code"),
                                                   "komunikat": e.get("message"), "metadata": e.get("metadata")}}
    if wiadomosc.get("refusal") or powod == "content_filter":
        return {**wynik, "powod": "odmowa", "tekst": wiadomosc.get("content"),
                "blad": {"rodzaj": "odmowa", **zakonczenie, "komunikat": wiadomosc.get("refusal")}}
    if powod == "length":
        return {**wynik, "powod": "uciecie", "tekst": wiadomosc.get("content"), "blad": {"rodzaj": "uciete", **zakonczenie}}
    if not wiadomosc.get("content"):
        return {**wynik, "powod": "blad", "blad": {"rodzaj": "pusta_tresc", **zakonczenie}}
    return {**wynik, "powod": "koniec", "tekst": wiadomosc["content"]}


ADAPTERY = {"openrouter": (openrouter_zapytanie, openrouter_odczyt)}


# --- Wspólne dla adapterów ---

def koszt_z_cennika(cennik, wejscie, wyjscie, z_pamieci):
    """USD z tokenów i cen za 1 mln tokenów (wyjście obejmuje myślenie); 0, gdy dostawca nie podał tokenów (np. odpowiedź z błędem)."""
    if wejscie is None or wyjscie is None:
        return 0.0
    z_pamieci = z_pamieci or 0
    return ((wejscie - z_pamieci) * cennik["wejscie"] + z_pamieci * cennik["z_pamieci"] + wyjscie * cennik["wyjscie"]) / 1e6


def _czekaj(funkcja, log):
    """Wykonuje funkcję w osobnym wątku, a główny co 1 s sprawdza, czy skończyła – dzięki temu Ctrl+C działa od razu
    (na Windows zablokowane requests.post ignoruje Ctrl+C do końca limitu czasu); co CO_ILE_POSTEP_S s komunikat na stderr."""
    wynik = {}

    def cel():
        try:
            wynik["odp"] = funkcja()
        except BaseException as e:  # także KeyboardInterrupt z atrapy w teście – przekazany do wątku głównego
            wynik["wyjatek"] = e
    watek = threading.Thread(target=cel, daemon=True)
    start = time.monotonic()
    watek.start()
    while watek.is_alive():
        watek.join(1)
        uplynelo = int(time.monotonic() - start)
        if watek.is_alive() and uplynelo and uplynelo % CO_ILE_POSTEP_S == 0:
            log.pokaz(f"czekam na odpowiedź… {uplynelo} s (limit {LIMIT_CZASU_S} s, Ctrl+C przerywa)")
    if "wyjatek" in wynik:
        raise wynik["wyjatek"]
    return wynik["odp"]


def _nie_wyslano(e):
    """Błąd przed wysłaniem zapytania (nie nawiązano połączenia z serwerem) – dostawca nic nie wygenerował ani nie policzył.
    Zerwanie już nawiązanego połączenia i przekroczony czas odczytu – zapytanie mogło dotrzeć (koszt niepewny)."""
    if isinstance(e, requests.ConnectTimeout):
        return True
    powod = getattr(e.args[0], "reason", None) if e.args else None
    return isinstance(powod, urllib3.exceptions.NewConnectionError)


def _bez_pustych(slownik):
    """Pola logu z wartością – log.json nie przyjmuje pustych tekstów w polach z listy."""
    return {k: v for k, v in slownik.items() if v not in (None, "")}


def stan_klucza(profil, env, log, http=requests):
    """Stan klucza u pośrednika (OpenRouter GET /api/v1/key): zużycie, limit, ile zostało w USD; bezpłatne.
    None przy błędzie albo braku adres_klucza – wtedy wywołujący nie wysyła zapytania."""
    dostawca = profil["dostawca_dane"]
    if not dostawca.get("adres_klucza"):
        log.zapisz("stan_klucza", "blad", powod="brak_adresu_klucza", szczegoly={"dostawca": profil["dostawca"]})
        return None
    try:
        odp = http.get(dostawca["adres_klucza"], headers={"Authorization": f"Bearer {env.get(dostawca['klucz_env'], '')}"}, timeout=30)
    except requests.RequestException as e:
        log.zapisz("stan_klucza", "blad", powod="polaczenie", szczegoly={"wyjatek": type(e).__name__})
        return None
    if odp.status_code != 200:
        log.zapisz("stan_klucza", "blad", powod="http", szczegoly={"http": odp.status_code, "tresc": odp.text[:300]})
        return None
    try:
        dane = odp.json()
    except ValueError:
        dane = None
    if not isinstance(dane, dict):  # np. strona HTML pośrednika – stan nieznany
        log.zapisz("stan_klucza", "blad", powod="odpowiedz_nie_json", szczegoly={"tresc": odp.text[:300]})
        return None
    dane = dane.get("data") or {}
    stan = {"zuzycie_usd": dane.get("usage"), "limit_usd": dane.get("limit"), "zostalo_usd": dane.get("limit_remaining")}
    log.zapisz("stan_klucza", "ok", szczegoly=stan)
    return stan


def pobierz_szczegoly(profil, wywolanie_id, env, log, http=requests, czekaj=time.sleep,
                      proby=SZCZEGOLY_ODCZYTY, odstep_s=SZCZEGOLY_ODSTEP_S):
    """Szczegóły generacji od pośrednika (OpenRouter: natywne tokeny dostawcy, w tym myślenie, czasy, koszt); bezpłatne.
    Statystyki pojawiają się z opóźnieniem – kilka prób odczytu co odstep_s (to nie ponowienie wywołania modelu)."""
    adres = profil["dostawca_dane"].get("adres_szczegolow")
    if not adres or not wywolanie_id:
        return None
    naglowki = {"Authorization": f"Bearer {env.get(profil['dostawca_dane']['klucz_env'], '')}"}
    for proba in range(1, proby + 1):
        czekaj(odstep_s)
        try:
            odp = http.get(adres, params={"id": wywolanie_id}, headers=naglowki, timeout=10)
        except requests.RequestException as e:
            log.zapisz("szczegoly", "ostrzezenie", request_id=wywolanie_id, szczegoly={"proba": proba, "wyjatek": type(e).__name__})
            continue
        if odp.status_code == 200:
            try:
                dane = odp.json()
            except ValueError:
                dane = None
            if not isinstance(dane, dict):  # np. strona HTML pośrednika – bez statystyk, wynik zostaje kompletny
                log.zapisz("szczegoly", "blad", request_id=wywolanie_id, powod="odpowiedz_nie_json",
                           szczegoly={"proba": proba, "tresc": odp.text[:300]})
                return None
            dane = dane.get("data") or {}
            log.zapisz("szczegoly", "ok", request_id=wywolanie_id, szczegoly={
                "proba": proba, "dostawca_faktyczny": dane.get("provider_name"), "natywne_wejscie": dane.get("native_tokens_prompt"),
                "natywne_wyjscie": dane.get("native_tokens_completion"), "natywne_myslenie": dane.get("native_tokens_reasoning"),
                "koszt_usd": dane.get("total_cost")})
            return dane
        log.zapisz("szczegoly", "ostrzezenie", request_id=wywolanie_id, szczegoly={"proba": proba, "http": odp.status_code, "tresc": odp.text[:300]})
    log.zapisz("szczegoly", "blad", request_id=wywolanie_id, powod="brak_szczegolow", szczegoly={"proby": proby})
    return None


def dopisz_szczegoly(profil, wynik, env, log, http=requests, czekaj=time.sleep):
    """Szczegóły generacji do wyniku wywolaj (natywne tokeny myślenia dokładniejsze niż usage). Ctrl+C przerywa samo czekanie
    na statystyki – wynik zostaje bez nich; zwraca True, gdy przerwano (wywołujący kończy uruchomienie po zapisaniu odpowiedzi)."""
    try:
        wynik["szczegoly"] = pobierz_szczegoly(profil, wynik["wywolanie_id"], env, log, http=http, czekaj=czekaj)
    except KeyboardInterrupt:
        log.zapisz("szczegoly", "blad", request_id=wynik["wywolanie_id"], powod="przerwane", komunikat="Ctrl+C w czasie czekania na szczegóły generacji")
        return True
    if wynik["szczegoly"] and wynik["szczegoly"].get("native_tokens_reasoning") is not None:
        wynik["tokeny_myslenie"] = wynik["szczegoly"]["native_tokens_reasoning"]
    return False


def wywolaj(profil, instrukcja, zlecenie, obrazy, schemat, env, log, http=requests, czekaj=time.sleep, szczegoly=True):
    """Jedno wywołanie modelu przez adapter dostawcy, z ponowieniem błędów przejściowych; zwraca ujednolicony wynik:
    ok (powod == koniec), powod, tekst, blad, profil/model/dostawca, wywolanie_id, dostawca_faktyczny, tokeny wejścia / wyjścia /
    myślenia / z pamięci (ostatniej próby), koszt_usd (większy z cennika i kwoty dostawcy, suma prób), koszt_dostawcy_usd (kwota dostawcy, suma prób), koszt_niepewny (próba bez
    odpowiedzi – dostawca mógł naliczyć koszt), ponowienia, czas_ms, usage i szczegoly dostawcy bez zmian, surowa (ostatnia odpowiedź).
    szczegoly=False – bez czekania na szczegóły generacji (wywołujący najpierw zapisuje odpowiedź, potem dopisz_szczegoly).
    http i czekaj – podmieniane w testach."""
    dostawca = profil["dostawca_dane"]
    zbuduj, odczytaj = ADAPTERY[dostawca["adapter"]]
    wynik = {"ok": False, "powod": "blad", "tekst": None, "blad": None,
             "profil": profil["nazwa"], "model": profil["model"], "dostawca": profil["dostawca"],
             "wywolanie_id": None, "dostawca_faktyczny": None,
             "tokeny_wejscie": None, "tokeny_wyjscie": None, "tokeny_myslenie": None, "tokeny_z_pamieci": None,
             "koszt_usd": 0.0, "koszt_dostawcy_usd": None, "koszt_niepewny": False, "wysylki_bez_odpowiedzi": 0, "ponowienia": 0, "czas_ms": 0,
             "usage": None, "szczegoly": None, "surowa": None}

    def zakoncz():
        """Linia wywolanie_api w logu (bez treści odpowiedzi) i wynik."""
        stan = "ok" if wynik["ok"] else ("ostrzezenie" if wynik["powod"] in ("uciecie", "odmowa") else "blad")
        log.zapisz("wywolanie_api", stan, **_bez_pustych({"powod": wynik["powod"], "request_id": wynik["wywolanie_id"],
                                                          "komunikat": (wynik["blad"] or {}).get("komunikat")}),
                   szczegoly={k: wynik[k] for k in ("profil", "model", "dostawca", "dostawca_faktyczny", "tokeny_wejscie", "tokeny_wyjscie",
                                                    "tokeny_myslenie", "tokeny_z_pamieci", "koszt_usd", "koszt_dostawcy_usd",
                                                    "koszt_niepewny", "wysylki_bez_odpowiedzi", "ponowienia", "czas_ms", "blad")})
        return wynik

    klucz = env.get(dostawca["klucz_env"], "")
    if not klucz:
        wynik["blad"] = {"rodzaj": "brak_klucza", "zmienna": dostawca["klucz_env"]}
        return zakoncz()
    # Twardy limit tokenów wyjścia (z myśleniem) z .env: bez wpisu nie wysyłamy; profil może tylko zejść niżej
    try:
        limit_wyjscia = int(env.get("MAX_OUTPUT_TOKENS", ""))
    except ValueError:
        wynik["blad"] = {"rodzaj": "brak_limitu_wyjscia", "zmienna": "MAX_OUTPUT_TOKENS"}
        return zakoncz()
    max_tokens = min(profil["max_tokens"], limit_wyjscia)
    tresc = zbuduj(profil, instrukcja, zlecenie, obrazy, schemat, max_tokens)
    naglowki = {"Authorization": f"Bearer {klucz}", "Content-Type": "application/json", **dostawca.get("naglowki", {})}

    for nr in range(len(OPOZNIENIA_S) + 1):
        log.pokaz(f"wysyłam: {profil['model']} przez {profil['dostawca']}, max_tokens {max_tokens}, próba {nr + 1}, limit czasu {LIMIT_CZASU_S} s")
        start = time.monotonic()
        surowa, wyslano = None, True
        try:
            odp = _czekaj(lambda: http.post(dostawca["adres"], headers=naglowki, json=tresc, timeout=LIMIT_CZASU_S), log)
            surowa = odp.text
            proba = odczytaj(odp.status_code, surowa)
            przejsciowy = bool(proba["blad"]) and (proba["blad"].get("http") in KODY_PRZEJSCIOWE
                                                   or proba["blad"].get("kod") in KODY_PRZEJSCIOWE)
        except KeyboardInterrupt:
            proba = {"powod": "blad", "blad": {"rodzaj": "przerwane", "komunikat":
                     "Ctrl+C w trakcie czekania; dostawca mógł dokończyć generację i naliczyć koszt – sprawdź panel"}}
            przejsciowy = False
        except requests.RequestException as e:
            proba = {"powod": "blad", "blad": {"rodzaj": "polaczenie", "wyjatek": type(e).__name__, "komunikat": str(e)[:500]}}
            przejsciowy = isinstance(e, (requests.Timeout, requests.ConnectionError))
            wyslano = not _nie_wyslano(e)
        wynik["czas_ms"] += round((time.monotonic() - start) * 1000)
        wynik["surowa"] = surowa
        if surowa is None and wyslano:  # wysyłka bez odpowiedzi – dostawca mógł policzyć; wywołujący może doliczyć rezerwę za każdą
            wynik["wysylki_bez_odpowiedzi"] += 1
            wynik["koszt_niepewny"] = True
        # większy z cennika i kwoty dostawcy – cennik nie zna zapisu do pamięci podręcznej
        wynik["koszt_usd"] += max(koszt_z_cennika(profil["cennik"], proba.get("tokeny_wejscie"), proba.get("tokeny_wyjscie"),
                                                  proba.get("tokeny_z_pamieci")), proba.get("koszt_dostawcy_usd") or 0.0)
        if proba.get("koszt_dostawcy_usd") is not None:
            wynik["koszt_dostawcy_usd"] = (wynik["koszt_dostawcy_usd"] or 0) + proba["koszt_dostawcy_usd"]
        if not przejsciowy or nr == len(OPOZNIENIA_S):
            break
        log.zapisz("ponowienie", "ostrzezenie", **_bez_pustych({"komunikat": proba["blad"].get("komunikat")}),
                   szczegoly={"proba": nr + 1, "przerwa_s": OPOZNIENIA_S[nr], "blad": proba["blad"]})
        wynik["ponowienia"] += 1
        try:
            czekaj(OPOZNIENIA_S[nr])
        except KeyboardInterrupt:  # Ctrl+C w przerwie – poprzednia wysyłka już zakończona błędem, nic nie czeka u dostawcy
            proba = {"powod": "blad", "blad": {"rodzaj": "przerwane", "komunikat": "Ctrl+C w przerwie przed ponowieniem błędu przejściowego"}}
            przejsciowy = False
            break

    if przejsciowy:  # ponowienia wyczerpane, błąd nie ustąpił – awaria sieci albo przeciążenie
        proba["blad"]["przejsciowy"] = True
    wynik.update({k: v for k, v in proba.items() if k in wynik and k not in ("koszt_dostawcy_usd",)})
    wynik["ok"] = wynik["powod"] == "koniec"
    if szczegoly:
        dopisz_szczegoly(profil, wynik, env, log, http=http, czekaj=czekaj)
    return zakoncz()
