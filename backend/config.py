# NanoLab — konfiguracja limitów i parametrów aplikacji
# Edytuj liczby tutaj; restart serwera wymagany po zmianie.

# ── PDF ──────────────────────────────────────────────────────────────
PDF_MAX_PAGES = 20
# Ile stron artykułu PDF jest wysyłanych do ekstrakcji tekstu.
# 20 stron ≈ 30 000 tokenów — wystarczy dla typowego artykułu naukowego.

# ── LLM ──────────────────────────────────────────────────────────────
LLM_MAX_TEXT_CHARS = 120_000
# Max liczba znaków tekstu wysyłanego do LLM w jednym wywołaniu.
# 120 000 znaków ≈ 30 000 tokenów. Przekroczenie → HTTP 400.

LLM_MAX_INSTRUCTION_CHARS = 100
# Max długość pola "hint for AI" (user_instruction).
# Przekroczenie → HTTP 400.

LLM_RATE_LIMIT_SECONDS = 30
# Minimalny odstęp (w sekundach) między wywołaniami /api/llm/call z tego samego IP.
# Zapobiega przypadkowemu wielokrotnemu klikaniu lub nadużyciu API.
# Ustaw 0 żeby wyłączyć rate limiting.
