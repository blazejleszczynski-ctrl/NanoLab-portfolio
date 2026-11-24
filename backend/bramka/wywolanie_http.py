# Bramka API w serwerze FastAPI: jedno wywołanie modelu przez bramka.wywolaj; wynik inny niż `koniec` → czytelny błąd HTTP.
# Wspólne dla routerów (analiza TEM, import PDF). Odczyt JSON z wyniku zostaje u wywołującego – każdy reaguje na zły JSON inaczej.
# Listy modeli dla formularzy: profile z polem zadanie (pdf / tem) w modele.json.
import json
import os
import pathlib

from fastapi import HTTPException

from bramka import bramka

MODELE_JSON = pathlib.Path(__file__).parent / "modele.json"


def modele_zadania(zadanie):
    """Profile danego zadania w kolejności z modele.json: [{id: model, label: etykieta, profil: nazwa profilu}]."""
    profile = json.loads(MODELE_JSON.read_text(encoding="utf-8"))["profile"]
    return [{"id": p["model"], "label": p["etykieta"], "profil": nazwa}
            for nazwa, p in profile.items() if p.get("zadanie") == zadanie]


def wywolaj_http(profil, instrukcja, zlecenie, obrazy, schemat, log, limit_env="MAX_OUTPUT_TOKENS"):
    """Wywołanie modelu bez czekania na szczegóły generacji; zwraca wynik bramki (ok) albo rzuca HTTPException 500/502/503.
    limit_env – zmienna .env z limitem tokenów wyjścia dla tego zadania (np. MAX_OUTPUT_TOKENS_ARTICLE dla importu PDF)."""
    env = dict(os.environ)
    if not env.get("OPENROUTER_API_KEY_VISION"):  # brak (albo pusty) klucz vision → klucz ogólny
        env["OPENROUTER_API_KEY_VISION"] = env.get("OPENROUTER_API_KEY", "")
    env["MAX_OUTPUT_TOKENS"] = env.get(limit_env, "")  # bramka czyta MAX_OUTPUT_TOKENS; brak zmiennej → nic nie idzie do API
    wynik = bramka.wywolaj(profil, instrukcja, zlecenie, obrazy, schemat, env, log, szczegoly=False)
    if wynik["ok"]:
        return wynik

    blad = wynik["blad"] or {}
    opis = f"{wynik['powod']} / {blad.get('rodzaj')}: {blad.get('komunikat') or blad.get('http') or ''}"
    if blad.get("rodzaj") == "brak_klucza":
        raise HTTPException(500, f"Konfiguracja backend/.env: brak {blad.get('zmienna')}")
    if blad.get("rodzaj") == "brak_limitu_wyjscia":
        raise HTTPException(500, f"Konfiguracja backend/.env: brak liczby w {limit_env}")
    if wynik["powod"] == "uciecie":
        raise HTTPException(502, f"Odpowiedź modelu ucięta – limit tokenów ({limit_env} w .env) za niski. {opis}")
    if blad.get("przejsciowy"):
        raise HTTPException(503, f"OpenRouter niedostępny po {wynik['ponowienia']} ponowieniach. {opis}")
    raise HTTPException(502, f"Błąd wywołania modelu: {opis}")
