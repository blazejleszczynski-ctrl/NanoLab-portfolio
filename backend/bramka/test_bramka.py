# Test bramki API (bramka.py) bez sieci i bez kosztu: atrapa HTTP podaje kolejne odpowiedzi albo wyjątki, atrapa czekania zapisuje przerwy.
# Sprawdza: kształt zapytania (wiadomości, obraz, schemat, wysiłek, max_tokens, nagłówki), rozpoznanie wyniku (koniec, ucięcie, odmowa, błędy),
# obraz z bajtów i detail high, zapytanie bez schematu i bez wysiłku (NanoLab),
# ponowienia błędów przejściowych (429, 502, 503, 504, czas, połączenie) – liczba wysyłek i przerwy dokładnie [3, 10], znacznik
# blad.przejsciowy po wyczerpaniu ponowień – oraz brak ponowień i znacznika przy pozostałych, sumę kosztu prób
# z cennika (wartości liczone ręcznie), szczegóły generacji, log (linie zgodne z log.json, klucz zamazany, bez treści odpowiedzi), pusty stdout.
# Test zakłada profil gpt61sol-or-medium z modele.json w obecnym kształcie – po zmianie profilu popraw oczekiwane wartości.
# Uruchomienie: python test_bramka.py (z folderu paczki albo z dowolnego miejsca)
import contextlib, io, json, pathlib, sys, tempfile
import requests

SKRYPTY = pathlib.Path(__file__).parent
sys.path.insert(0, str(SKRYPTY))
import bramka, log_ndjson

KLUCZ = "sk-or-v1-0123456789abcdef"
ENV = {"OPENROUTER_API_KEY": KLUCZ, "MAX_OUTPUT_TOKENS": "16000"}
SCHEMAT = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"], "additionalProperties": False}
# bajty z nagłówkiem PNG – wystarczą, bo bramka koduje bajty, nie dekoduje obrazu
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class Odp:
    """Odpowiedź HTTP z atrapy: kod i tekst."""
    def __init__(self, kod, tresc):
        self.status_code = kod
        self.text = tresc if isinstance(tresc, str) else json.dumps(tresc)

    def json(self):
        return json.loads(self.text)


class Http:
    """Atrapa requests: post podaje kolejne pozycje z listy (Odp albo wyjątek do rzucenia), get – szczegóły generacji."""
    def __init__(self, posty, get=None):
        self.posty, self.get_odp, self.wyslane, self.pobrane = list(posty), get, [], []

    def post(self, adres, headers, json, timeout):
        self.wyslane.append({"adres": adres, "naglowki": headers, "tresc": json, "timeout": timeout})
        pozycja = self.posty.pop(0)
        if isinstance(pozycja, BaseException):
            raise pozycja
        return pozycja

    def get(self, adres, params=None, headers=None, timeout=None):
        self.pobrane.append({"adres": adres, "params": params})
        return self.get_odp


def odpowiedz(tresc='{"a": 1}', powod="stop", usage=None, **wiadomosc):
    """Treść odpowiedzi chat/completions OpenRoutera z jednym wyborem."""
    return {"id": "gen-1", "provider": "OpenAI", "usage": usage if usage is not None else USAGE,
            "choices": [{"finish_reason": powod, "native_finish_reason": powod, "message": {"content": tresc, **wiadomosc}}]}


USAGE = {"prompt_tokens": 1000, "completion_tokens": 500, "cost": 0.0066,
         "prompt_tokens_details": {"cached_tokens": 200}, "completion_tokens_details": {"reasoning_tokens": 300}}
KOSZT_USAGE = (800 * 2 + 200 * 0.10 + 500 * 10) / 1e6  # cennik 2 / 10 / 0,10 za 1 mln, liczone ręcznie = 0,00662


def profil(szczegoly=False):
    """Profil GPT z modele.json; bez adresu szczegółów, chyba że test go sprawdza."""
    p = bramka.wczytaj_profil(SKRYPTY / "modele.json", "gpt61sol-or-medium", {"etap": "test"})
    if not szczegoly:
        p["dostawca_dane"].pop("adres_szczegolow")
    return p


def uruchom(katalog, posty, env=ENV, get=None, szczegoly=False, czekaj_c=False, w_wywolaniu=True):
    """Jedno wywołanie bramki z atrapami; zwraca (wynik, atrapa http, przerwy, linie logu, stdout). czekaj_c – Ctrl+C w pierwszej przerwie;
    szczegoly – profil z adresem szczegółów generacji; w_wywolaniu – argument szczegoly funkcji wywolaj."""
    obraz = katalog / "o.png"
    obraz.write_bytes(PNG)
    log = log_ndjson.Log(katalog / "logi", "test_bramka")
    http, przerwy, wyjscie = Http(posty, get), [], io.StringIO()

    def czekaj(s):
        przerwy.append(s)
        if czekaj_c:
            raise KeyboardInterrupt

    with contextlib.redirect_stdout(wyjscie), contextlib.redirect_stderr(io.StringIO()):
        wynik = bramka.wywolaj(profil(szczegoly), "INSTRUKCJA", "ZLECENIE", [obraz], SCHEMAT, env, log, http=http, czekaj=czekaj,
                               szczegoly=w_wywolaniu)
    linie = [json.loads(l) for l in log.sciezka.read_text(encoding="utf-8").splitlines()] if log.sciezka.exists() else []
    log.sciezka.unlink(missing_ok=True)
    return wynik, http, przerwy, linie, wyjscie.getvalue()


def main():
    wyniki = []

    def sprawdz(nazwa, warunek):
        wyniki.append(bool(warunek))
        print(f"  {'ok' if warunek else 'NIEZGODNE'}: {nazwa}")

    with tempfile.TemporaryDirectory() as tmp:
        k = pathlib.Path(tmp)

        # 1. Wynik poprawny i kształt zapytania
        w, h, p, linie, out = uruchom(k, [Odp(200, odpowiedz())])
        t = h.wyslane[0]["tresc"]
        sprawdz("koniec: ok, tekst, tokeny, id, dostawca", w["ok"] and w["powod"] == "koniec" and w["tekst"] == '{"a": 1}'
                and (w["tokeny_wejscie"], w["tokeny_wyjscie"], w["tokeny_myslenie"], w["tokeny_z_pamieci"]) == (1000, 500, 300, 200)
                and w["wywolanie_id"] == "gen-1" and w["dostawca_faktyczny"] == "OpenAI" and w["blad"] is None)
        sprawdz("koszt z cennika = 0,00662 (ręcznie), kwota dostawcy obok, pewny", abs(w["koszt_usd"] - KOSZT_USAGE) < 1e-12
                and w["koszt_dostawcy_usd"] == 0.0066 and w["koszt_niepewny"] is False)
        sprawdz("bez ponowień: 1 wysyłka, przerwy []", len(h.wyslane) == 1 and p == [] and w["ponowienia"] == 0)
        sprawdz("zapytanie: system = instrukcja, user = obraz potem zlecenie",
                t["messages"][0] == {"role": "system", "content": "INSTRUKCJA"}
                and [c["type"] for c in t["messages"][1]["content"]] == ["image_url", "text"]
                and t["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
                and t["messages"][1]["content"][1]["text"] == "ZLECENIE")
        sprawdz("zapytanie: schemat bez zmian, strict", t["response_format"]["json_schema"]["schema"] == SCHEMAT
                and t["response_format"]["json_schema"]["strict"] is True)
        sprawdz("zapytanie: wysilek medium → reasoning.effort, max_tokens = min(32000, 16000)",
                t["reasoning"] == {"effort": "medium"} and t["max_tokens"] == 16000 and t["model"] == "openai/gpt-6.1-sol")
        sprawdz("zapytanie: parametry dostawcy bez zmian", t["provider"]["only"] == ["openai"] and t["provider"]["data_collection"] == "deny")
        sprawdz("nagłówki: klucz i nazwa etapu", h.wyslane[0]["naglowki"]["Authorization"] == f"Bearer {KLUCZ}"
                and h.wyslane[0]["naglowki"]["X-OpenRouter-Title"] == "test")
        sprawdz("stdout pusty", out == "")
        sprawdz("log: 1 linia wywolanie_api ok, bez treści odpowiedzi",
                [(l["krok"], l["wynik"]) for l in linie] == [("wywolanie_api", "ok")] and '{"a": 1}' not in json.dumps(linie))

        # 2. Wyniki bez ponowienia
        przypadki = [
            ("ucięcie", [Odp(200, odpowiedz(powod="length"))], "uciecie", "uciete", "ostrzezenie"),
            ("odmowa (refusal)", [Odp(200, odpowiedz(tresc=None, refusal="nie"))], "odmowa", "odmowa", "ostrzezenie"),
            ("odmowa (content_filter)", [Odp(200, odpowiedz(powod="content_filter"))], "odmowa", "odmowa", "ostrzezenie"),
            ("pusta treść", [Odp(200, odpowiedz(tresc=""))], "blad", "pusta_tresc", "blad"),
            ("błąd w wyborze", [Odp(200, {"id": "g", "choices": [{"finish_reason": "error", "error": {"code": 500, "message": "x"}}]})],
             "blad", "blad_w_odpowiedzi", "blad"),
            ("błąd w treści HTTP 200", [Odp(200, {"error": {"code": 400, "message": "zły schemat"}})], "blad", "blad_w_tresci_http200", "blad"),
            ("HTTP 400", [Odp(400, {"error": {"code": 400, "message": "zły"}})], "blad", "http", "blad"),
            ("odpowiedź nie JSON", [Odp(500, "<html>internal error</html>")], "blad", "odpowiedz_nie_json", "blad"),
            ("Ctrl+C", [KeyboardInterrupt()], "blad", "przerwane", "blad"),
        ]
        for nazwa, posty, powod, rodzaj, stan in przypadki:
            w, h, p, linie, out = uruchom(k, posty)
            sprawdz(f"{nazwa}: powod {powod}, rodzaj {rodzaj}, 1 wysyłka, bez przerw, log {stan}, nieprzejściowy",
                    not w["ok"] and w["powod"] == powod and w["blad"]["rodzaj"] == rodzaj and len(h.wyslane) == 1 and p == []
                    and [(l["krok"], l["wynik"]) for l in linie] == [("wywolanie_api", stan)] and "przejsciowy" not in w["blad"])
        w, *_ = uruchom(k, [Odp(200, odpowiedz(powod="length", tresc='{"a"'))])
        sprawdz("ucięcie: ucięty tekst zostaje w wyniku", w["tekst"] == '{"a"')
        w, *_ = uruchom(k, [KeyboardInterrupt()])
        sprawdz("Ctrl+C: koszt niepewny", w["koszt_niepewny"] is True)

        # 3. Ponowienia błędów przejściowych
        b503 = lambda: Odp(503, {"error": {"code": 503, "message": "przeciążenie"}})
        w, h, p, linie, _ = uruchom(k, [b503(), b503(), Odp(200, odpowiedz())])
        sprawdz("503, 503, ok: 3 wysyłki, przerwy [3, 10], ponowienia 2, ok", len(h.wyslane) == 3 and p == [3, 10]
                and w["ok"] and w["ponowienia"] == 2)
        sprawdz("ponowienia w logu: 2 × ponowienie/ostrzezenie, potem wywolanie_api ok",
                [(l["krok"], l["wynik"]) for l in linie] == [("ponowienie", "ostrzezenie")] * 2 + [("wywolanie_api", "ok")]
                and [l["szczegoly"]["przerwa_s"] for l in linie[:2]] == [3, 10])
        w, h, p, *_ = uruchom(k, [b503(), b503(), b503()])
        sprawdz("503 × 3: 3 wysyłki, przerwy [3, 10], błąd http 503", len(h.wyslane) == 3 and p == [3, 10]
                and not w["ok"] and w["blad"]["http"] == 503 and w["ponowienia"] == 2)
        sprawdz("503 × 3: błąd przejściowy po wyczerpaniu ponowień", w["blad"].get("przejsciowy") is True)
        for kod in (502, 504):  # przejściowe obok 429 i 503
            w, h, p, *_ = uruchom(k, [Odp(kod, {"error": {"code": kod, "message": "brama"}})] * 3)
            sprawdz(f"{kod} × 3: 3 wysyłki, przerwy [3, 10], przejściowy", len(h.wyslane) == 3 and p == [3, 10]
                    and w["blad"]["http"] == kod and w["blad"].get("przejsciowy") is True)
        w, h, p, *_ = uruchom(k, [Odp(502, "<html>bad gateway</html>"), Odp(200, odpowiedz())])
        sprawdz("502 ze stroną HTML, ok: 2 wysyłki, przerwy [3], ok", len(h.wyslane) == 2 and p == [3] and w["ok"])
        w, h, p, *_ = uruchom(k, [requests.Timeout("czas")] * 3)
        sprawdz("przekroczony czas × 3: 3 wysyłki, przejściowy", len(h.wyslane) == 3 and w["blad"].get("przejsciowy") is True)
        w, h, p, *_ = uruchom(k, [b503()], czekaj_c=True)
        sprawdz("Ctrl+C w przerwie po 503: przerwane, nieprzejściowy", w["blad"]["rodzaj"] == "przerwane" and "przejsciowy" not in w["blad"])
        w, h, p, *_ = uruchom(k, [Odp(429, {"error": {"code": 429, "message": "limit"}}), Odp(200, odpowiedz())])
        sprawdz("429, ok: 2 wysyłki, przerwy [3]", len(h.wyslane) == 2 and p == [3] and w["ok"])
        w, h, p, *_ = uruchom(k, [Odp(200, {"error": {"code": 503, "message": "x"}}), Odp(200, odpowiedz())])
        sprawdz("HTTP 200 z błędem 503 w treści: ponowienie", len(h.wyslane) == 2 and p == [3] and w["ok"])
        w, h, p, *_ = uruchom(k, [requests.Timeout("czas"), Odp(200, odpowiedz())])
        sprawdz("przekroczony czas, ok: przerwy [3], ok, koszt niepewny", p == [3] and w["ok"] and w["koszt_niepewny"] is True)
        w, h, p, *_ = uruchom(k, [requests.ConnectionError("x")] * 3)
        sprawdz("zerwane połączenie × 3: 3 wysyłki, przerwy [3, 10], rodzaj polaczenie, przejściowy", len(h.wyslane) == 3 and p == [3, 10]
                and w["blad"]["rodzaj"] == "polaczenie" and w["koszt_niepewny"] is True and w["blad"].get("przejsciowy") is True)
        w, h, p, *_ = uruchom(k, [requests.exceptions.InvalidURL("x")])
        sprawdz("inny błąd requests: bez ponowienia, nieprzejściowy", len(h.wyslane) == 1 and p == [] and w["blad"]["rodzaj"] == "polaczenie"
                and "przejsciowy" not in w["blad"])
        drogi = {**USAGE, "cost": 0.001}
        w, *_ = uruchom(k, [Odp(200, {"id": "g0", "error": {"code": 503, "message": "x"}, "usage": drogi}), Odp(200, odpowiedz())])
        sprawdz("koszt = suma prób (cennik 2 × 0,00662, dostawca 0,001 + 0,0066)", abs(w["koszt_usd"] - 2 * KOSZT_USAGE) < 1e-12
                and abs(w["koszt_dostawcy_usd"] - 0.0076) < 1e-12 and w["koszt_niepewny"] is False)
        zapis_do_pamieci = {**USAGE, "cost": 0.0070, "prompt_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 300}}
        w, *_ = uruchom(k, [Odp(503, {"error": {"code": 503, "message": "x"}, "usage": drogi}), Odp(200, odpowiedz(usage=zapis_do_pamieci))])
        sprawdz(f"koszt = większy z cennika i dostawcy w każdej wysyłce – zapis do pamięci podręcznej (jest {w['koszt_usd']:.5f})",
                abs(w["koszt_usd"] - (KOSZT_USAGE + 0.0070)) < 1e-12 and abs(w["koszt_dostawcy_usd"] - 0.0080) < 1e-12)

        # 4. Odmowa przed wysyłką
        w, h, *_ = uruchom(k, [], env={"MAX_OUTPUT_TOKENS": "16000"})
        sprawdz("brak klucza: 0 wysyłek", h.wyslane == [] and w["blad"]["rodzaj"] == "brak_klucza")
        w, h, *_ = uruchom(k, [], env={"OPENROUTER_API_KEY": KLUCZ})
        sprawdz("brak MAX_OUTPUT_TOKENS: 0 wysyłek", h.wyslane == [] and w["blad"]["rodzaj"] == "brak_limitu_wyjscia")

        # 5. Szczegóły generacji (OpenRouter /generation)
        gen = Odp(200, {"data": {"native_tokens_reasoning": 444, "provider_name": "OpenAI", "total_cost": 0.0066}})
        w, h, p, linie, _ = uruchom(k, [Odp(200, odpowiedz())], get=gen, szczegoly=True)
        sprawdz("szczegóły: 1 pobranie po przerwie 3 s, natywne myślenie 444 zamiast 300", h.pobrane[0]["params"] == {"id": "gen-1"}
                and p == [3] and w["tokeny_myslenie"] == 444 and w["szczegoly"]["provider_name"] == "OpenAI")
        w, h, p, linie, _ = uruchom(k, [Odp(200, odpowiedz())], get=Odp(404, "brak"), szczegoly=True)
        sprawdz("szczegóły niedostępne: 10 odczytów co 3 s (do 30 s), wynik ok z tokenami z usage", len(h.pobrane) == 10
                and p == [3] * 10 and w["ok"]
                and w["tokeny_myslenie"] == 300 and linie[-2]["krok"] == "szczegoly" and linie[-2]["wynik"] == "blad")
        w, h, p, linie, _ = uruchom(k, [Odp(200, odpowiedz())], get=gen, szczegoly=True, w_wywolaniu=False)
        sprawdz("szczegoly=False: 0 pobrań, bez przerw – wywołujący zapisuje odpowiedź przed szczegółami",
                h.pobrane == [] and p == [] and w["szczegoly"] is None and w["ok"])

        # 6. Log: klucz zamazany
        w, h, p, linie, _ = uruchom(k, [Odp(401, {"error": {"code": 401, "message": f"zły klucz {KLUCZ}"}})])
        sprawdz("klucz w komunikacie dostawcy zamazany w logu", KLUCZ not in json.dumps(linie) and "***" in linie[0]["komunikat"])

        # 7. Profil
        zly = json.loads((SKRYPTY / "modele.json").read_text(encoding="utf-8"))
        zly["profile"]["gpt61sol-or-medium"]["wysilek"] = "max"
        (k / "zle.json").write_text(json.dumps(zly), encoding="utf-8")
        try:
            bramka.wczytaj_profil(k / "zle.json", "gpt61sol-or-medium", {"etap": "test"})
            odrzucony = False
        except ValueError:
            odrzucony = True
        sprawdz("wysilek spoza low/medium/high: odmowa wczytania profilu", odrzucony)

        # 8. Dopasowanie do NanoLab: obraz w pamięci, zapytanie bez schematu, model bez myślenia (funkcja bez sieci)
        JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
        obraz = bramka.openrouter_zapytanie(profil(), "I", "Z", [JPEG], SCHEMAT, 100)["messages"][1]["content"][0]["image_url"]
        sprawdz("obraz z bajtów JPEG: data:image/jpeg, detail high",
                obraz["url"].startswith("data:image/jpeg;base64,/9j/") and obraz["detail"] == "high")
        sprawdz("obraz z pliku: detail high", t["messages"][1]["content"][0]["image_url"]["detail"] == "high")
        sprawdz("bez schematu: brak response_format",
                "response_format" not in bramka.openrouter_zapytanie(profil(), "I", "Z", [], None, 100))
        bez_wysilku = json.loads((SKRYPTY / "modele.json").read_text(encoding="utf-8"))
        del bez_wysilku["profile"]["gpt61sol-or-medium"]["wysilek"]
        (k / "bez_wysilku.json").write_text(json.dumps(bez_wysilku), encoding="utf-8")
        p4o = bramka.wczytaj_profil(k / "bez_wysilku.json", "gpt61sol-or-medium", {"etap": "test"})
        sprawdz("profil bez wysilku: wczytany, brak reasoning w zapytaniu",
                "reasoning" not in bramka.openrouter_zapytanie(p4o, "I", "Z", [], SCHEMAT, 100))

    print(f"test_bramka: {sum(wyniki)}/{len(wyniki)}")
    return 0 if all(wyniki) else 1


if __name__ == "__main__":
    sys.exit(main())
