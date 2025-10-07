#!/usr/bin/env python3
import sys
import os
import locale
from datetime import datetime
import re
import pdfplumber
import traceback
import io
import unicodedata

# obsługa polskich znaków w konsoli
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
except Exception:
    pass


def parse_pdf_text(pdf_path):
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:
        import traceback
        print("❌ ERROR: Nie udało się otworzyć PDF:", pdf_path)
        traceback.print_exc()
        raise



def remove_diacritics(text):
    if not text:
        return ""
    text = text.replace('ł', 'l').replace('Ł', 'L')
    nkfd = unicodedata.normalize('NFKD', text)
    no_comb = "".join([c for c in nkfd if not unicodedata.combining(c)])
    # zachowaj caret ^ i znaki ASCII; inne znaki zastąp spacją
    allowed = set(chr(i) for i in range(32, 127)) | {'^'}
    cleaned = ''.join(ch if ch in allowed else ' ' for ch in no_comb)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def clean_amount(amount):
    if not amount:
        return "0,00"
    s = str(amount)
    s = s.replace('\xa0', '').replace(' ', '')
    # usuń separatory tysięcy (kropki), zamień przecinek na kropkę aby zrobić float
    s = s.replace('.', '').replace(',', '.')
    try:
        val = float(s)
    except Exception:
        s2 = re.sub(r'[^0-9\.\-]', '', s)
        try:
            val = float(s2) if s2 else 0.0
        except:
            val = 0.0
    return "{:.2f}".format(val).replace('.', ',')


def format_account_for_25(acc_raw):
    if not acc_raw:
        return "/PL00000000000000000000000000"
    acc = re.sub(r'\s+', '', acc_raw).upper()
    if acc.startswith('PL') and len(acc) == 28:
        return f"/{acc}"
    if not acc.startswith('PL') and re.match(r'^\d{26}$', acc):
        return f"/PL{acc}"
    if not acc.startswith('/'):
        return f"/{acc}"
    return acc


def split_description(desc, first_len=120, next_len=65):
    d = remove_diacritics(desc or "")
    # zachowaj sekwencje ^.. jako tokeny
    parts = re.split(r'(\^[0-9]{2}[^^]*)', d)
    tokens = [p.strip() for p in parts if p and p.strip()]
    if not tokens:
        return ["BRAK OPISU"]
    # zbuduj pierwszą linię łącznie z tokenami do first_len
    first = ""
    i = 0
    while i < len(tokens) and (not first or len(first) + 1 + len(tokens[i]) <= first_len):
        first = (first + " " + tokens[i]).strip()
        i += 1
    segs = [first] if first else []
    rest = "".join(tokens[i:]) if i < len(tokens) else ""
    while rest:
        segs.append(rest[:next_len])
        rest = rest[next_len:]
    return [s.strip() for s in segs if s.strip()]


def enrich_desc_for_86(desc):
    if not desc:
        return ""
    d = desc
    # wyciągnij IBAN (PLxxxx...) z dowolnymi spacjami i dodaj na początek
    iban_m = re.search(r'(PL[\s-]?\d{2}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{2})', desc, re.IGNORECASE)
    if iban_m:
        iban = re.sub(r'[\s-]+', '', iban_m.group(1)).upper()
        if not d.startswith(iban):
            d = iban + " " + d
    else:
        # jeśli jest 26 cyfr bez PL, dopisz PL prefix i dodaj na początek
        digits26 = re.search(r'(?<!\d)(\d{26})(?!\d)', re.sub(r'\s+', '', desc))
        if digits26:
            acct = digits26.group(1)
            pref = "PL" + acct
            if not d.startswith(pref):
                d = pref + " " + d

    # wydobądź numer faktury/ref i dołącz na końcu, jeśli istnieje
    ref_patterns = [
        r'(Nr ref\s*[:\.]?\s*[A-Z0-9\/\-\._]+)',
        r'(F\/\d+[^\s]*)',
        r'(FAKTUR[AY]\s*[A-Z0-9\/\-\._]*)',
        r'(SACC\s*\d+)',
        r'(Faktura\s*[:\s]*[A-Z0-9\/\-\._]+)'
    ]
    refs = []
    for p in ref_patterns:
        m = re.search(p, desc, re.IGNORECASE)
        if m:
            refs.append(m.group(1).strip())
    if refs:
        for r in refs:
            if r.upper() not in d.upper():
                d = (d + " " + r).strip()

    # próbuj wydobyć nazwę kontrahenta: fragmenty z dużych liter i spacje, ogranicz długość
    caps = re.findall(r'\b[A-ZĄĆĘŁŃÓŚŹŻ]{3,}(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ0-9\.\,\-]{2,}){0,6}', desc)
    if caps:
        for c in caps:
            if not re.search(r'PRZELEW|FAKTURA|SACC|NR|NUMER|IBAN|PL', c, re.IGNORECASE):
                if c.strip() and c.strip().upper() not in d.upper():
                    d = (d + " " + c.strip()).strip()
                    break

    d = re.sub(r'\s+', ' ', d).strip()
    return d


def extract_statement_dates(text, transactions):
    if not text:
        today = datetime.today()
        return today.strftime("%y%m%d"), today.strftime("%y%m%d")
    t = re.sub(r'[\t\r ]+', ' ', text)
    m = re.search(
        r'od\s+(\d{2}[./-]\d{2}[./-]\d{4}|\d{4}-\d{2}-\d{2})\s*(?:do|-)\s*(\d{2}[./-]\d{2}[./-]\d{4}|\d{4}-\d{2}-\d{2})',
        t, re.IGNORECASE)
    if m:
        def norm(d):
            d = d.replace('.', '-').replace('/', '-')
            for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
                try:
                    return datetime.strptime(d, fmt).strftime("%y%m%d")
                except:
                    pass
            return None
        a = norm(m.group(1)); b = norm(m.group(2))
        if a and b:
            return a, b
    m1 = re.search(r'od\s+(\d{2}[./-]\d{2}[./-]\d{4})', text, re.IGNORECASE)
    m2 = re.search(r'do\s+(\d{2}[./-]\d{2}[./-]\d{4})', text, re.IGNORECASE)
    if m1 and m2:
        try:
            a = datetime.strptime(m1.group(1).replace('.', '-').replace('/', '-'), "%d-%m-%Y").strftime("%y%m%d")
            b = datetime.strptime(m2.group(1).replace('.', '-').replace('/', '-'), "%d-%m-%Y").strftime("%y%m%d")
            return a, b
        except:
            pass
    if transactions:
        return transactions[0][0], transactions[-1][0]
    today = datetime.today().strftime("%y%m%d")
    return today, today


def extract_statement_number(text):
    if not text:
        return None
    m = re.search(r':28C:\s*0*([0-9]{1,6})', text)
    if m:
        return m.group(1).zfill(6)
    m2 = re.search(r'Numer\s+wyci[aą]gu\s*[:\-]?\s*0*([0-9]{1,6})(?:/(\d{4}))?', text, re.IGNORECASE)
    if m2:
        return m2.group(1).zfill(6)
    m3 = re.search(r'wyci[aą]g(?:\s+nr|\s+nr\.)?\s*[:\-]?\s*0*([0-9]{1,6})', text, re.IGNORECASE)
    if m3:
        return m3.group(1).zfill(6)
    return None


def build_mt940(account_number, saldo_pocz, saldo_konc, transactions):
    today = datetime.today().strftime("%y%m%d")
    start_date = transactions[0][0] if transactions else today
    end_date = transactions[-1][0] if transactions else today

    # override via metadata if set
    start_date = getattr(build_mt940, "_stmt_start", start_date)
    end_date = getattr(build_mt940, "_stmt_end", end_date)
    ref = getattr(build_mt940, "_orig_ref", None) or datetime.now().strftime("%Y%m%d%H%M%S")[:16]
    stmt_no = getattr(build_mt940, "_stmt_no", None)

    acct = re.sub(r'\s+', '', (account_number or '')).upper()
    only = re.sub(r'\D', '', acct)
    if only.startswith('PL'):
        only = only[2:]
    if len(only) == 26:
        tag25 = f":25:/PL{only}"
    else:
        tag25 = f":25:{format_account_for_25(account_number)}"

    if stmt_no:
        digits = re.sub(r'\D', '', str(stmt_no))
        digits = digits[-6:].zfill(6)
        tag28 = f":28C:{digits}"
    else:
        tag28 = ":28C:000001"

    def cd_and_amount(s):
        s = (s or "").strip()
        if s.startswith('-'):
            return 'D', s.lstrip('-').replace(' ', '')
        return 'C', s.replace(' ', '')

    cd60, amt60 = cd_and_amount(saldo_pocz)
    cd62, amt62 = cd_and_amount(saldo_konc)

    lines = [
        f":20:{ref}",
        tag25,
        tag28,
        f":60F:{cd60}{start_date}PLN{amt60}"
    ]

    for date, amount, desc in transactions:
        txn_type = 'D' if amount.startswith('-') else 'C'
        amt_clean = amount.lstrip('-').replace(' ', '')

        # determine entry date: prefer explicit second date in description, otherwise duplicate booking MMDD
        entry = ''
        m_date2 = re.search(r'(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})', desc or '')
        if m_date2:
            raw2 = m_date2.group(1)
            try:
                if '/' in raw2:
                    entry = datetime.strptime(raw2, "%d/%m/%Y").strftime("%m%d")
                else:
                    entry = datetime.strptime(raw2, "%Y-%m-%d").strftime("%m%d")
            except:
                entry = ''
        if not entry:
            entry = date[2:]  # duplicate booking MMDD

        # Determine transaction code: prefer explicit Nxxx, caret-style codes, or heuristics
        ncode_m = re.search(r'\bN\s*0*?(\d{2,3})\b', desc or '', re.IGNORECASE)
        if not ncode_m:
            ncode_m = re.search(r'\^(\d{2,3})\^', desc or '')
        if ncode_m:
            txn_code = ncode_m.group(1).zfill(3)
        else:
            if re.search(r'PODZIELON|ZUS|KRUS', desc or '', re.IGNORECASE):
                txn_code = '562'
            elif re.search(r'INTERNET|M/B|P4', desc or '', re.IGNORECASE):
                txn_code = '775'
            elif re.search(r'ELIXIR|EXPRESS', desc or '', re.IGNORECASE):
                txn_code = '178'
            elif re.search(r'PRZELEW KRAJOWY MI', desc or '', re.IGNORECASE):
                txn_code = '240'
            else:
                txn_code = '641' if txn_type == 'D' else '240'

        # build :61 with entry
        lines.append(f":61:{date}{entry}{txn_type}{amt_clean}N{txn_code}NONREF")

        # enrich description before splitting to :86
        enriched = enrich_desc_for_86(desc or "")
        segs = split_description(enriched)
        for seg in segs:
            lines.append(f":86:{seg}")

    lines.append(f":62F:{cd62}{end_date}PLN{amt62}")
    lines.append("-")
    return "\n".join(lines) + "\n"


def save_mt940_file(mt940_text, output_path):
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    # zapis CP1250 (windows-1250) — Symfonia oczekuje tego kodowania
    with open(output_path, "w", encoding="windows-1250", errors="replace") as f:
        f.write(mt940_text)


def extract_statement_month(transactions):
    if not transactions:
        return "Nieznany"
    try:
        locale.setlocale(locale.LC_TIME, "pl_PL.UTF-8")
        first_date = datetime.strptime(transactions[0][0], "%y%m%d")
        return first_date.strftime("%B %Y")
    except:
        return "Nieznany"


def santander_parser(text):
    text_norm = text.replace('\xa0', ' ')
    parts = re.split(r'(?i)Data operacji', text_norm)
    blocks = parts[1:] if len(parts) > 1 else []
    transactions = []

    date_re = re.compile(r'(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})')
    pln_re = re.compile(r'([+-]?\d{1,3}(?:[ \u00A0]\d{3})*[.,]\d{2})\s*PLN', re.IGNORECASE)

    for blk in blocks:
        date_m = date_re.search(blk)
        if not date_m:
            continue

        raw_date = date_m.group(1)
        try:
            if '/' in raw_date:
                date = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%y%m%d")
            else:
                date = datetime.strptime(raw_date, "%Y-%m-%d").strftime("%y%m%d")
        except:
            date = datetime.today().strftime("%y%m%d")

        plns = pln_re.findall(blk)
        if not plns:
            continue

        raw_amount = plns[0]
        sign = ''
        amt_search_re = re.compile(re.escape(raw_amount))
        m_idx = amt_search_re.search(blk)
        if m_idx:
            idx = m_idx.start()
            prev = blk[max(0, idx - 3):idx]
            if '-' in prev:
                sign = '-'
        if not sign:
            if re.search(r'(obci[aą]rzenie|wyp[ał]ta|PODZIELONY DO ZUS|PODZIELONY DO KRUS|PRZELEW)', blk, re.IGNORECASE):
                if re.search(r'(PODZIELONY DO ZUS|PODZIELONY DO KRUS)', blk, re.IGNORECASE):
                    sign = '-'

        amt_str = (sign + raw_amount).replace(' ', '').replace('\xa0', '')
        amt_clean = clean_amount(amt_str)
        amt_signed = ('-' + amt_clean) if sign == '-' else amt_clean

        desc_part = blk[:date_m.start()]
        desc = re.sub(r'\s+', ' ', desc_part).strip()

        transactions.append((date, amt_signed, desc))

    saldo_pocz_m = re.search(
        r"Saldo początkowe na dzień[:\s]*([0-9\/\-]{8,10})\s*([-\d\s,\.]+)\s*PLN",
        text_norm,
        re.IGNORECASE
    )
    saldo_konc_m = re.search(
        r"Saldo końcowe na dzień[:\s]*([0-9\/\-]{8,10})\s*([-\d\s,\.]+)\s*PLN",
        text_norm,
        re.IGNORECASE
    )
    saldo_pocz = clean_amount(saldo_pocz_m.group(2)) if saldo_pocz_m else "0,00"
    saldo_konc = clean_amount(saldo_konc_m.group(2)) if saldo_konc_m else "0,00"

    account_m = re.search(r'(\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4})', text_norm)
    account = account_m.group(1).replace(' ', '') if account_m else "00000000000000000000000000"

    return account, saldo_pocz, saldo_konc, transactions


def pekao_parser(text):
    text_norm = text.replace('\xa0', ' ').replace('\u00A0', ' ')
    # salda
    saldo_pocz_m = re.search(r"SALDO POCZ(Ą|A)TKOWE\s+([-\d\s,\.]+)", text_norm, re.IGNORECASE)
    saldo_konc_m = re.search(r"SALDO KO(Ń|N)COWE\s+([-\d\s,\.]+)", text_norm, re.IGNORECASE)
    saldo_pocz = clean_amount(saldo_pocz_m.group(2)) if saldo_pocz_m else "0,00"
    saldo_konc = clean_amount(saldo_konc_m.group(2)) if saldo_konc_m else "0,00"
    # konto
    account_m = re.search(r'(\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4})', text_norm)
    account = account_m.group(1).replace(' ', '') if account_m else "00000000000000000000000000"

    transactions = []
    seen = set()

    # 1) Linia-po-linii
    pattern_inline = re.compile(r'(\d{2}/\d{2}/\d{4})\s+([+-]?\d{1,3}(?:[ \u00A0]\d{3})*[.,]\d{2})\s+(.+)')
    date_only_re = re.compile(r'^\d{2}/\d{2}/\d{4}$')
    amount_re = re.compile(r'([+-]?\d{1,3}(?:[ \u00A0]\d{3})*[.,]\d{2})')

    lines = text_norm.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m_inline = pattern_inline.match(line)
        if m_inline:
            raw_date, amt_str, desc = m_inline.groups()
            try:
                date = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%y%m%d")
            except:
                i += 1
                continue
            amt_clean = clean_amount(amt_str)
            if '-' in amt_str and not amt_clean.startswith('-'):
                amt_clean = '-' + amt_clean
            # normalize desc: usuń page headery które mogą się wciąć do opisu
            desc_norm = re.sub(r'Strona\s*\d+/\d+|Wyszczegolnienie transakcji|Data waluty|Kwota|Opis operacji', '', desc, flags=re.IGNORECASE).strip()
            key = (date, amt_clean, (desc_norm or "")[:120])
            if key not in seen:
                transactions.append((date, amt_clean, desc_norm))
                seen.add(key)
            i += 1
            continue

        if date_only_re.match(line):
            raw_date = line
            try:
                date = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%y%m%d")
            except:
                i += 1
                continue
            amt_clean = "0,00"
            desc_parts = []
            # następna linia może zawierać kwotę
            if i + 1 < len(lines):
                amt_match = amount_re.search(lines[i + 1])
                if amt_match:
                    amt_str = amt_match.group(1)
                    amt_clean = clean_amount(amt_str)
                    if '-' in amt_str and not amt_clean.startswith('-'):
                        amt_clean = '-' + amt_clean
                    i += 1
            j = i + 1
            while j < len(lines) and not date_only_re.match(lines[j].strip()) and not pattern_inline.match(lines[j].strip()):
                desc_parts.append(lines[j].strip())
                j += 1
            description = " ".join(desc_parts)
            description = re.sub(r'Strona\s*\d+/\d+|Wyszczegolnienie transakcji|Data waluty|Kwota|Opis operacji', '', description, flags=re.IGNORECASE).strip()
            key = (date, amt_clean, (description or "")[:120])
            if key not in seen:
                transactions.append((date, amt_clean, description))
                seen.add(key)
            i = j
            continue

        i += 1

    # 2) Globalny skan dokumentu (uchwyci transakcje złamane między stronami)
    global_pattern = re.compile(
        r'(\d{2}/\d{2}/\d{4})'                       # data
        r'([^\n]{0,120}?)'                           # filler
        r'([+-]?\d{1,3}(?:[ \u00A0]\d{3})*[.,]\d{2})' # kwota
        r'(?:[^\S\r\n]*\n)?'                         # optional newline gap
        r'(.{0,500}?)'                               # opis (większy fragment)
        r'(?=(?:\n\d{2}/\d{2}/\d{4})|\Z)',           # lookahead
        re.DOTALL
    )

    for m in global_pattern.finditer(text_norm):
        raw_date = m.group(1)
        raw_amt = m.group(3)
        raw_desc = m.group(4).strip().replace('\n', ' ')
        try:
            date = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%y%m%d")
        except:
            continue
        amt_clean = clean_amount(raw_amt)
        if '-' in raw_amt and not amt_clean.startswith('-'):
            amt_clean = '-' + amt_clean
        raw_desc = re.sub(r'Strona\s*\d+/\d+|Wyszczegolnienie transakcji|Data waluty|Kwota|Opis operacji', '', raw_desc, flags=re.IGNORECASE).strip()
        key = (date, amt_clean, (raw_desc or "")[:120])
        if key not in seen:
            transactions.append((date, amt_clean, raw_desc))
            seen.add(key)

    # 3) Post-processing dedupe: grupuj po (date, amount) i wybierz najdłuższy opis
    grouped = {}
    for d, a, desc in transactions:
        k = (d, a)
        desc_norm = (desc or "").strip()
        # jeśli opis zawiera tylko krótkie nagłówki, traktuj jako pusty
        if re.fullmatch(r'(PRZELEW|PRZELEW KRAJOWY|PRZELEW INTERNET|PRZELEW PODZIELONY DO ZUS/KRUS)?', desc_norm, flags=re.IGNORECASE):
            desc_norm = ""
        if k not in grouped:
            grouped[k] = desc_norm
        else:
            # wybierz dłuższy, bardziej szczegółowy opis
            if len(desc_norm) > len(grouped[k]):
                grouped[k] = desc_norm

    deduped = []
    for (d, a), desc in grouped.items():
        deduped.append((d, a, desc))

    # 4) Sortuj transakcje po dacie i kwocie dla stabilności
    def txn_sort_key(t):
        try:
            return (int(t[0]), abs(float(t[1].replace(',', '.'))))
        except:
            return (int(t[0]) if t[0].isdigit() else 0, 0)
    deduped.sort(key=txn_sort_key)

    return account, saldo_pocz, saldo_konc, deduped


def mbank_parser(text):
    raise NotImplementedError("Parser mBank jeszcze niezaimplementowany.")


BANK_PARSERS = {
    "santander": santander_parser,
    "mbank": mbank_parser,
    "pekao": pekao_parser
}


def detect_bank(text):
    if not text:
        return None
    t = text.lower()
    # jeśli plik już jest MT940, zwróć specjalny typ "mt940"
    if ":20:" in text and ":25:" in text and ":61:" in text:
        return "mt940"
    if "santander" in t or "data operacji" in t:
        return "santander"
    if "bank pekao" in t or "pekao" in t or ("saldo początkowe" in t and "saldo końcowe" in t):
        return "pekao"
    if "mbank" in t or "m-bank" in t:
        return "mbank"
    compact = re.sub(r'\s+', '', text.lower())
    if re.search(r'\bpl\d{26}\b', compact) or re.search(r'\b\d{26}\b', compact):
        if 'elixir' in t or 'saldo pocz' in t or 'saldo konc' in t:
            return "pekao"
        return "santander"
    return None


def sanity_check(saldo_pocz, saldo_konc, transactions):
    def to_float(s): return float(s.replace(' ', '').replace(',', '.'))
    try:
        s_p = to_float(saldo_pocz)
        s_k = to_float(saldo_konc)
    except Exception:
        return False, "Niepoprawny format sald"
    total = 0.0
    for _, amt, _ in transactions:
        try:
            v = float(amt.lstrip('-').replace(',', '.'))
        except Exception:
            return False, "Nieprawidłowa kwota w transakcjach"
        total += (-v if amt.startswith('-') else v)
    if abs((s_p + total) - s_k) > 0.02:
        return False, f"Rozbieżność sald: pocz {s_p} + suma {total} != konc {s_k}"
    return True, "OK"


def convert(pdf_path, output_path):
    text = parse_pdf_text(pdf_path)

    bank = detect_bank(text)
    print(f"🔍 Wykryty bank: {bank}")
    if not bank or (bank not in BANK_PARSERS and bank != "mt940"):
        raise ValueError("Nie rozpoznano banku lub parser niezaimplementowany.")

    # jeśli wejście już jest w formacie MT940, zapisz je bez parsowania
    if bank == "mt940":
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="windows-1250", errors="replace") as f:
            f.write(text if isinstance(text, str) else text.decode("utf-8", errors="replace"))
        print("✅ Wejście wygląda jak MT940. Zapisano plik wynikowy bez parsowania.")
        return

    account, saldo_pocz, saldo_konc, transactions = BANK_PARSERS[bank](text)
    statement_month = extract_statement_month(transactions)
    print(f"📅 Miesiąc wyciągu: {statement_month}")
    print(f"📄 Liczba transakcji: {len(transactions)}")
    if not transactions:
        print("⚠️ Brak transakcji w pliku PDF.")

    # attach extracted metadata for build_mt940
    stmt_start, stmt_end = extract_statement_dates(text, transactions)
    stmt_no = extract_statement_number(text)
    orig_ref_m = re.search(r':20:\s*([^\r\n]+)', text)
    orig_ref = orig_ref_m.group(1).strip() if orig_ref_m else None

    build_mt940._stmt_start = stmt_start
    build_mt940._stmt_end = stmt_end
    build_mt940._stmt_no = stmt_no
    build_mt940._orig_ref = orig_ref

    # sanity_check: logujemy ostrzeżenie, ale nie blokujemy zapisu
    try:
        ok, msg = sanity_check(saldo_pocz, saldo_konc, transactions)
    except Exception as e:
        ok, msg = False, f"Sanity check error: {e}"
    if not ok:
        print(f"⚠️ Sanity check: {msg} (zapis będzie kontynuowany; rozważ ręczną weryfikację)")

    mt940_text = build_mt940(account, saldo_pocz, saldo_konc, transactions)
    save_mt940_file(mt940_text, output_path)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Użycie: python converter_web.py input.pdf output.mt940")
        sys.exit(1)

    input_pdf = sys.argv[1]
    output_mt940 = sys.argv[2]

    try:
        convert(input_pdf, output_mt940)
        print("✅ Konwersja zakończona sukcesem.")
    except Exception as e:
        print(f"❌ Błąd: {e}")
        traceback.print_exc()
        sys.exit(1)
