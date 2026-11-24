# Wspólny moduł logu NDJSON dla skryptów (jedna linia = jedno zdarzenie, umowa: schematy/log.json).
# Archiwizuje poprzedni przebieg (bez nadpisywania i bez blokady startu), zamazuje sekrety (klucz API, nazwa użytkownika w ścieżkach) w pliku i w terminalu,
# zapisuje niezłapany wyjątek jako koniec/blad, czyta plik logu po liniach \n (czytaj) i wykrywa uruchomienia bez kroku koniec.
# W terminalu (tylko gdy stderr to terminal) kolor i ikonka według kroku i wyniku, reszta linii szara, profil biały – plik logu bez zmian.
import datetime, json, pathlib, re, subprocess, sys, time, traceback, uuid
from jsonschema import Draft202012Validator

# Klucze API w całości (Anthropic sk-ant-…, pośrednicy sk-or-…) i dokładna nazwa katalogu profilu po Users z separatorem
# \, / albo - (C:\Users\<nazwa>, /c/Users/<nazwa>, c--Users-<nazwa>-…); po nazwie nie może stać litera, cyfra ani _ (interpunkcja może);
# przed kluczem nie może stać litera ani cyfra (disk-ant-farm to słowo); reszta tekstu zostaje
WZORZEC_KLUCZA = re.compile(r"(?<![^\W_])sk-(?:ant|or)-[A-Za-z0-9_\-]+")
WZORZEC_UZYTKOWNIKA = re.compile(rf"(?i)(?:[a-z]:|/[a-z])?[\\/\-]+users[\\/\-]+{re.escape(pathlib.Path.home().name)}(?!\w)")
POLA_WSPOLNE = ("czas", "uruchomienie_id", "skrypt", "wersja", "krok", "wynik")
# Wygląd linii w terminalu (kolor ANSI, ikonka) – najpierw według kroku, potem według wyniku; wzór: AI_devs 4, 02_01, helpers/logger.js
STYL_KROKU = {"wysylka": ("\x1b[34m", "↑")}  # niebieski – zapytanie wysłane
STYL_WYNIKU = {"ok": ("\x1b[32m", "✓"), "ostrzezenie": ("\x1b[38;5;208m", "⚠"), "blad": ("\x1b[31m", "✗")}  # zielony, pomarańczowy, czerwony
SZARY, RESET = "\x1b[2m", "\x1b[0m"  # reszta linii szara
# Wartości tych pól zostają białe – w zdarzeniu (profil="x") i w szczegółach ("profil": "x"); dopisz własne pola
WYROZNIONE = re.compile(r'(\b(?:profil)=|"(?:profil)": )("[^"]*")')
_WALIDATOR = Draft202012Validator(json.loads(
    (pathlib.Path(__file__).parent / "schematy" / "log.json").read_text(encoding="utf-8")))


def zamaz(wartosc):
    """Zamazuje sekrety w tekstach, także zagnieżdżonych w słownikach i listach; ścieżki zamienia na tekst."""
    if isinstance(wartosc, pathlib.PurePath):
        wartosc = str(wartosc)
    if isinstance(wartosc, str):
        return WZORZEC_UZYTKOWNIKA.sub("~", WZORZEC_KLUCZA.sub("***", wartosc))
    if isinstance(wartosc, dict):
        return {zamaz(k): zamaz(v) for k, v in wartosc.items()}
    if isinstance(wartosc, (list, tuple)):
        return [zamaz(v) for v in wartosc]
    return wartosc


def wersja_kodu():
    """Skrót commita repozytorium, w którym leży moduł; None, gdy git niedostępny."""
    try:
        wynik = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=pathlib.Path(__file__).parent,
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return wynik.stdout.strip() if wynik.returncode == 0 else None


def czytaj(sciezka):
    """Plik NDJSON → (pełne linie, czy na końcu jest ścięty fragment). Podział tylko na \\n – U+2028, U+0085 i podobne zostają
    w linii; każda linia dekodowana osobno, więc ucięty znak wielobajtowy we fragmencie nie psuje reszty."""
    *pelne, fragment = pathlib.Path(sciezka).read_bytes().split(b"\n")
    linie = []
    for nr, bajty in enumerate(pelne, 1):
        try:
            linie.append(bajty.decode("utf-8"))
        except UnicodeDecodeError as e:
            raise ValueError(f"linia {nr}: zły UTF-8 ({e})") from None
    return linie, bool(fragment.strip())


def bez_konca(sciezka):
    """Id uruchomień, które mają krok start, a nie mają koniec – skrypt padł bez śladu (np. zabity proces).
    Ścięty fragment na końcu (proces zabity w trakcie zapisu) to przebieg bez koniec: gdy ostatni start ma już koniec albo startu
    nie ma, fragment był startem nowego przebiegu – id „?”. Linia w środku, która nie jest zdarzeniem, to błąd."""
    starty, konce = [], set()
    linie, sciety = czytaj(sciezka)
    for nr, linia in enumerate(linie, 1):
        if not linia.strip():
            continue
        try:
            zdarzenie = json.loads(linia)
        except json.JSONDecodeError as e:
            raise ValueError(f"linia {nr}: nie jest JSON-em ({e})") from None
        if not isinstance(zdarzenie, dict) or "uruchomienie_id" not in zdarzenie:
            raise ValueError(f"linia {nr}: nie jest zdarzeniem z uruchomienie_id")
        if zdarzenie.get("krok") == "start":
            starty.append(zdarzenie["uruchomienie_id"])
        elif zdarzenie.get("krok") == "koniec":
            konce.add(zdarzenie["uruchomienie_id"])
    wynik = [u for u in starty if u not in konce]
    if sciety and (not starty or starty[-1] in konce):
        wynik.append("?")
    return wynik


class Log:
    """Log jednego uruchomienia skryptu: <katalog>/<skrypt>.ndjson; poprzedni plik idzie do <katalog>/archiwum/."""

    def __init__(self, katalog, skrypt):
        """Nowe id uruchomienia i wersja kodu; poprzedni plik logu tego skryptu od razu do archiwum."""
        self.sciezka = pathlib.Path(katalog) / f"{skrypt}.ndjson"
        self.sciezka.parent.mkdir(parents=True, exist_ok=True)
        self.skrypt = skrypt
        self.uruchomienie_id = uuid.uuid4().hex[:12]
        self.wersja = wersja_kodu()
        self._start = time.monotonic()
        self._archiwizuj_poprzedni()

    def _archiwizuj_poprzedni(self):
        """Przenosi poprzedni przebieg do archiwum/RRRR-MM-DD_HHMMSS_<skrypt>.ndjson – czas z jego pierwszej linii, a gdy
        jest uszkodzona, z modyfikacji pliku; zajęta nazwa → sufiks _2, _3… (nic nie nadpisane, start nie blokowany)."""
        if not self.sciezka.exists() or self.sciezka.stat().st_size == 0:
            return
        try:
            with open(self.sciezka, encoding="utf-8") as f:
                czas = datetime.datetime.fromisoformat(json.loads(f.readline())["czas"])  # np. 2026-10-24T14:02:11.305+02:00
        except (ValueError, KeyError, TypeError):  # ucięta albo obca pierwsza linia
            czas = datetime.datetime.fromtimestamp(self.sciezka.stat().st_mtime)
        katalog, rdzen = self.sciezka.parent / "archiwum", f"{czas:%Y-%m-%d_%H%M%S}_{self.skrypt}"
        katalog.mkdir(exist_ok=True)
        cel, nr = katalog / f"{rdzen}.ndjson", 1
        while cel.exists():
            nr += 1
            cel = katalog / f"{rdzen}_{nr}.ndjson"
        self.sciezka.rename(cel)

    def zapisz(self, krok, wynik, **pola):
        """Dopisuje zdarzenie do pliku i pokazuje je w terminalu; linia niezgodna z log.json to błąd programu (ValueError)."""
        zdarzenie = zamaz({"czas": datetime.datetime.now().astimezone().isoformat(timespec="milliseconds"),
                           "uruchomienie_id": self.uruchomienie_id, "skrypt": self.skrypt, "wersja": self.wersja,
                           "krok": krok, "wynik": wynik, **pola})
        blad = next(iter(sorted(_WALIDATOR.iter_errors(zdarzenie), key=str)), None)
        if blad:
            raise ValueError(f"linia logu niezgodna z log.json: {blad.message}")
        linia = json.dumps(zdarzenie, ensure_ascii=False, allow_nan=False)  # NaN/Infinity → ValueError przed otwarciem pliku
        with open(self.sciezka, "a", encoding="utf-8") as f:
            f.write(linia + "\n")
        pola_txt = " ".join(f"{k}={json.dumps(v, ensure_ascii=False)[:120]}" for k, v in zdarzenie.items()
                            if k not in POLA_WSPOLNE)
        self.pokaz(f"{krok} [{wynik}] {pola_txt}"[:400], STYL_KROKU.get(krok) or STYL_WYNIKU.get(wynik))

    def start(self, **szczegoly):
        """Pierwsza linia uruchomienia: argumenty wywołania i ustawienia."""
        self.zapisz("start", "ok", szczegoly={"argumenty": sys.argv[1:], **szczegoly})

    def koniec(self, wynik, **pola):
        """Ostatnia linia uruchomienia; czas trwania dopisany do szczegoly."""
        szczegoly = {**pola.pop("szczegoly", {}), "czas_trwania_s": round(time.monotonic() - self._start, 3)}
        self.zapisz("koniec", wynik, szczegoly=szczegoly, **pola)

    def przechwyc_wyjatki(self):
        """Niezłapany wyjątek (i Ctrl+C) trafia do logu jako koniec/blad z pełnym śladem; w terminalu ślad już zamazany."""
        def hak(typ, wartosc, slad):
            """Zastępuje sys.excepthook: najpierw linia koniec/blad, potem ślad w terminalu (także gdy zapis do logu zawiedzie)."""
            slad_txt = zamaz("".join(traceback.format_exception(typ, wartosc, slad)))
            powod = "przerwanie" if issubclass(typ, KeyboardInterrupt) else "wyjatek"
            try:
                self.koniec("blad", powod=powod, blad=slad_txt)
            finally:
                print(slad_txt, file=sys.stderr, end="", flush=True)
        sys.excepthook = hak

    def pokaz(self, tekst, styl=None):
        """Tylko terminal (stderr), bez zapisu do pliku – postęp, który nie jest zdarzeniem; tekst zamazany.
        Kolory tylko w terminalu (przy przekierowaniu do pliku i w testach wydruk bez zmian): styl (kolor, ikonka) obejmuje ikonkę
        i „krok [wynik]”, reszta linii szara, wartości pól WYROZNIONE białe; ikonka tylko przy UTF-8 (cp1250 nie ma ✓ – wydruk padłby)."""
        czas, tekst = f"{datetime.datetime.now():%H:%M:%S}", zamaz(tekst)
        if not sys.stderr.isatty():
            print(f"{czas} {self.skrypt} | {tekst}", file=sys.stderr, flush=True)
            return
        naglowek = ""
        if styl:
            kolor, ikona = styl
            utf8 = (sys.stderr.encoding or "").lower().replace("-", "") == "utf8"
            koniec = tekst.find("]") + 1  # „krok [wynik]” – początek tekstu z zapisz
            naglowek, tekst = f"{kolor}{ikona + ' ' if utf8 else ''}{tekst[:koniec]}{RESET}", tekst[koniec:]
        tekst = WYROZNIONE.sub(lambda m: f"{m[1]}{RESET}{m[2]}{SZARY}", tekst)
        print(f"{SZARY}{czas} {self.skrypt} |{RESET} {naglowek}{SZARY}{tekst}{RESET}", file=sys.stderr, flush=True)
