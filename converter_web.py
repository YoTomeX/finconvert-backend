#!/usr/bin/env python3
import sys, re, io, traceback, unicodedata, logging
from datetime import datetime
import pdfplumber
import argparse

# Ustawienia logowania
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

# Ustawienie kodowania wyjścia (dla konsoli)
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
except Exception:
    pass

# Definicja pól SWIFT, które mogą zakończyć blok transakcji
HEADERS_BREAK = (':20:', ':25:', ':28C:', ':60F:', ':62F:', ':64:', '-')

def parse_pdf_text(pdf_path):
    """Parsuje tekst ze wszystkich stron PDF."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:
        logging.error(f"Błąd otwierania lub parsowania PDF: {e}")
        return ""

def remove_diacritics(text):
    """
    Konwertuje polskie znaki diakrytyczne na ich łacińskie odpowiedniki 
    (np. ł -> l, ń -> n, ę -> e). Zapewnia to maksymalną kompatybilność 
    ze starszymi systemami księgowymi w Polsce i konwertuje na wielkie litery.
    """
    if not text: return ""
    
    # 1. Traktowanie ł/Ł i normalizacja NFKD
    text = text.replace('ł','l').replace('Ł','L')
    nkfd = unicodedata.normalize('NFKD', text)
    no_comb = "".join([c for c in nkfd if not unicodedata.combining(c)])
    
    # 2. Wymiana reszty (dla bezpieczeństwa)
    replacements = {
        'ą': 'a', 'ć': 'c', 'ę': 'e', 'ń': 'n', 
        'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
        'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ń': 'N', 
        'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z',
    }
    for old, new in replacements.items():
        no_comb = no_comb.replace(old, new)
        
    # 3. Usuwanie niedozwolonych znaków
    # Poprawiony regex, aby przepuszczał % (często używany w opisach)
    cleaned = re.sub(r'[^A-Z0-9\s,\.\-/\(\)\?\:\+\r\n\%]', ' ', no_comb.upper()) 
    
    # 4. Usunięcie nadmiarowych spacji
    return re.sub(r'\s+',' ',cleaned).strip()

def clean_amount(amount):
    """Czysci kwotę do formatu numerycznego (przecinek jako separator dziesiętny)."""
    # Ulepszone czyszczenie kwoty: pozwala na spacje i różne separatory
    s = str(amount).replace('\xa0','').replace('.','').replace(' ','').replace('`', '') 
    # Jeśli format to X.XXX,XX lub X XXX,XX, zamieniamy kropkę na brak
    if re.search(r'\d\.\d{3}', s):
        s = s.replace('.', '')
    # Następnie zamieniamy ewentualny ostatni przecinek na kropkę
    s = s.replace(',', '.')

    try:
        val = float(s)
    except Exception:
        val = 0.0
    return "{:.2f}".format(val).replace('.', ',')

def pad_amount(amt, width=11):
    """Uzupełnia kwotę zerami wiodącymi do formatu MT940 (np. 00000012,34)."""
    try:
        amt = amt.replace(' ', '').replace('\xa0','')
        if ',' not in amt:
            amt = amt + ',00'
        
        # Usuń znak '-' do paddingu, dodaj go na końcu, jeśli był
        is_negative = amt.startswith('-')
        if is_negative:
            amt = amt.lstrip('-')
            
        left, right = amt.split(',')
        # Długość paddingu = Całkowita szerokość - długość części dziesiętnej - przecinek
        left = left.zfill(width - len(right) - 1)
        
        final_amt = f"{left},{right}"
        # W MT940 znak debetu/kredytu jest w Field 61, a nie w kwocie.
        return final_amt 
    except Exception as e:
        logging.warning("pad_amount error: %s -> %s", e, amt)
        return '0'.zfill(width-3)+',00'

def format_account_for_25(acc_raw):
    """Formatuje numer rachunku do pola :25:."""
    if not acc_raw: return "/PL00000000000000000000000000"
    acc = re.sub(r'\s+','',acc_raw).upper()
    if acc.startswith('PL') and len(acc)==28: return f"/{acc}"
    if re.match(r'^\d{26}$', acc): return f"/PL{acc}"
    if not acc.startswith('/'): return f"/{acc}"
    return acc

def extract_mt940_headers(text):
    """Ekstrahuje numery wyciągu z tekstu PDF."""
    num_20 = datetime.now().strftime('%y%m%d%H%M%S') # Ref. transakcji (unikalny)
    num_28C = '00001' # Numer wyciągu (domyślnie)

    # Szukamy numeru wyciągu/strony (często jest to 4 cyfry, np. 0001)
    m28c = re.search(r'(Numer wyciągu|Nr wyciągu|Wyciąg nr|Wyciąg nr\.\s+)\s*[:\-]?\s*(\d{4})[\/\-]?\d{4}', text, re.I)
    if m28c: 
        num_28C = m28c.group(2).zfill(5)
    else:
        # Fallback: szukamy 'Strona X/Y' i bierzemy X
        page_match = re.search(r'Strona\s*(\d+)/\d+', text)
        if page_match:
            num_28C = page_match.group(1).zfill(5)
            
    return num_20, num_28C

def map_transaction_code(desc):
    """Mapuje polski opis na kod transakcji SWIFT (MT940 Field 61, po N)."""
    desc_clean = remove_diacritics(desc)
    desc_lower = desc_clean.lower()
    
    # 1. Kody bankowe (NXXX)
    if 'PRZELEW KRAJOWY' in desc_clean: return 'N240'
    if 'OBCIAZENIE RACHUNKU' in desc_clean: return 'N495' # Ogólne obciążenie
    if 'POBRANIE OPLATY' in desc_clean or 'PROWIZJA' in desc_clean: return 'N775'
    if 'WPLATA ZASILENIE' in desc_clean: return 'N524'
    if 'CZEK' in desc_clean: return 'N027'
    
    # 2. Kody specyficzne dla odbiorców (do wstawienia w //)
    if 'ZUS' in desc_clean or 'KRUS' in desc_clean: return 'N562'
    if 'PODZIELONY' in desc_clean: return 'N641'
    
    # 3. Domyślny / Inne
    return 'NTRF'

def segment_description(desc, code):
    """Segmentuje opis transakcji do ustrukturyzowanego pola :86:."""
    
    # Usuń diakrytyki i oczyść z niepotrzebnych znaków
    desc = remove_diacritics(desc)

    # Usunięcie stopki i nagłówków ze środka opisu
    stopka_keywords = [
        "BANK POLSKA KASA OPIEKI", "GWARANCJA BFG", "WWW.PEKAO.COM.PL",
        "KAPITAL ZAKLADOWY", "SAD REJONOWY", "NR KRS", "NIP:",
        "OPROCENTOWANIE", "ARKUSZ INFORMACYJNY", "INFORMACJA DOTYCZACA TRYBU"
    ]
    desc_upper = desc.upper()
    for kw in stopka_keywords:
        pos = desc_upper.find(kw)
        if pos != -1:
            desc = desc[:pos].strip()
            break

    segments = []
    seen = set()

    def add_segment(prefix, value):
        if not value: return
        
        clean_value = str(value).strip()
        
        # Filtrujemy kontrolne znaki ASCII
        clean_value = re.sub(r'[\x00-\x1f]+', ' ', clean_value).strip()
        
        # Ograniczenie długości MT940 do 65 znaków na wiersz. 
        # Pole 86 pozwala na wiele wierszy. Ograniczamy wartość segmentu do max 250 znaków.
        if len(clean_value) > 250:
            clean_value = clean_value[:250].rsplit(' ', 1)[0]
        
        # Zabezpieczenie przed duplikatami (używamy prefixu i krótkiego kawałka tekstu jako klucza)
        key = f"{prefix}{clean_value[:50]}"
        if key not in seen:
            # Pole 86 w MT940 nie używa ^, jeśli opis jest jednoliniowy i krótki, ale dla struktury jest to bezpieczniejsze.
            # Zmieniamy ^ na //, które jest bezpieczniejsze w polskiej interpretacji MT940.
            segments.append(f"/{prefix}{clean_value}") 
            seen.add(key)

    # 1. Segment KOD TRANSAKCJI (np. /NTRF)
    # Zapewniamy, że kod z Field 61 jest w segmencie 86
    segments.append(f"/{code[1:]}") # Używamy TRF zamiast NTRF
    
    # 2. IBAN Odbiorcy/Nadawcy (/38)
    ibans = re.findall(r'(PL\d{26})', desc)
    for iban in ibans:
        add_segment("38", iban)

    # 3. Numer Referencyjny / Numer Dokumentu (/20)
    # Wzmacniamy wyszukiwanie numeru referencyjnego/dokumentu
    ref = re.search(r'(NR REF[ .:]|FAKTURA NR|FAKTURA)[:\s\.]*([A-Z0-9\/\-]+)', desc, re.I)
    if ref: 
        add_segment("20", ref.group(2)) # Bierze tylko faktyczną referencję
    
    # 4. Nazwa Kontrahenta (/32)
    # Szukanie nazwy kontrahenta jest trudne, ulepszamy wzorzec, aby szukał po słowach kluczowych
    # np. DLA: NAZWA KONTRAHENTA
    name_match = re.search(r'(DLA:|OD:|T:)\s*([A-Z][A-Z\s\.\,\-\']{5,100})', desc)
    if name_match:
        val = name_match.group(2).strip()
        add_segment("32", val)
            
    # 5. Główny opis, jeśli nie ma /00 (Cała reszta)
    if not any(s.startswith("/00") for s in segments):
        add_segment("00", desc)

    return segments

def remove_trailing_86(mt940_text):
    """Usuwa linie :86:, które nie są poprzedzone linią :61: (błąd parsowania)."""
    lines = mt940_text.strip().split('\n')
    result = []
    valid_transaction = False
    for line in lines:
        if line.startswith(':61:'):
            valid_transaction = True
            result.append(line)
        elif line.startswith(':86:'):
            if valid_transaction:
                result.append(line)
            else:
                logging.warning("Pomijam linię :86: bez poprzedniego :61: w nagłówku.")
        elif any(line.startswith(h) for h in HEADERS_BREAK):
            valid_transaction = False
            result.append(line)
        else:
            result.append(line)
    return "\n".join(result) + "\n"

def deduplicate_transactions(transactions):
    """Usuwa zduplikowane transakcje (np. powtarzające się na różnych stronach PDF)."""
    seen = set()
    out = []
    for t in transactions:
        # Klucz to Data, Kwota, i kawałek opisu (bezpieczny)
        key = (t[0], t[1], t[2][:50])
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out

def pekao_parser(text):
    """
    Parser dla wyciągów Pekao, wykorzystujący bardziej precyzyjne dopasowanie
    transakcji, aby uniknąć błędów parsowania opisu.
    
    Kluczowe poprawki: Wzmacniamy regex na transakcje.
    """
    account = ""; saldo_pocz = "0,00"; saldo_konc = "0,00"
    transactions = []
    
    # 1. Parsowanie nagłówków
    num_20, num_28C = extract_mt940_headers(text)
    
    # 2. Parsowanie konta i sald
    lines = text.splitlines()
    for line in lines:
        # Parsowanie konta
        acc = re.search(r'(PL\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4})', line)
        if acc: account = re.sub(r'\s+', '', acc.group(1))
        
        # Poprawione parsowanie salda: pozwala na dowolną liczbę spacji/separatorów
        sp = re.search(r'SALDO POCZĄTKOWE\s*[:\-]?\s*([\-\s\d,]+)', line, re.I)
        if sp: saldo_pocz = clean_amount(sp.group(1))
        sk = re.search(r'SALDO KOŃCOWE\s*[:\-]?\s*([\-\s\d,]+)', line, re.I)
        if sk: saldo_konc = clean_amount(sk.group(1))
        
    # 3. Parsowanie transakcji
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        
        # WZORZEC A (Ulepszony):
        # Szukamy linii zaczynającej się od DD/MM/RRRR, po której następuje
        # dowolny tekst (opis), a na końcu kwota z opcjonalnym znakiem - i walutą PLN.
        # Ten wzorzec jest najbardziej prawdopodobny dla transakcji w jednej linii.
        # (\d{2}\/\d{2}\/\d{4}) - Data Waluty (lub Operacji)
        # (.*?) - Opis transakcji (leniwy, aby złapać tylko to, co trzeba)
        # ([\-]?\s*[\d\s,]+\.?\d*) - Kwota (opcjonalny minus, cyfry, spacje, przecinki/kropki)
        # (PLN\s*)$ - Waluta na końcu linii
        
        m_a = re.match(r'^(\d{2}\/\d{2}\/\d{4})\s+(.*?)\s+([\-]?\s*[\d\s,]+\.?\d*)\s*PLN\s*$', line, re.I)
        
        dt = None
        amt = None
        
        if m_a:
            dt_raw = m_a.group(1)
            desc_part = m_a.group(2)
            amt_raw = m_a.group(3)
            
            # Wzrost niezawodności: upewnij się, że opis nie jest kolejną datą (np. Data Operacji | Data Waluty | Opis)
            if re.match(r'\d{2}\/\d{2}\/\d{4}', desc_part.strip().split()[-1] if desc_part.strip() else ''):
                # Odrzucamy, jeśli ostatnia część opisu to data (sugeruje to format tabelaryczny z datą waluty)
                m_a = None 
            else:
                dt = datetime.strptime(dt_raw, "%d/%m/%Y").strftime("%y%m%d")
                amt = clean_amount(amt_raw)
                
                # Zbieranie opisu transakcji, która mogła się zacząć w wierszu m_a
                desc_lines = [desc_part.strip()]
                
                # Przechodzenie do następnej linii w poszukiwaniu kontynuacji opisu
                j = i + 1
                while j < len(lines) and not re.match(r'^\d{2}\/\d{2}\/\d{4}', lines[j].strip()) and lines[j].strip():
                    desc_lines.append(lines[j].strip())
                    j += 1
                
                # Używamy pełnego opisu
                desc = " ".join(desc_lines).strip()
                transactions.append((dt, amt, desc))
                i = j # Kontynuujemy od nowej transakcji lub końca opisu
                continue
        
        i += 1
        
    transactions.sort(key=lambda x: x[0])
    return account, saldo_pocz, saldo_konc, deduplicate_transactions(transactions), num_20, num_28C

def build_mt940(account, saldo_pocz, saldo_konc, transactions, num_20="1", num_28C="00001", today=None):
    """Buduje finalny plik MT940 na podstawie sparsowanych danych."""
    if today is None:
        today = datetime.today().strftime("%y%m%d")

    # Formatowanie numeru konta
    acct = format_account_for_25(account)

    # Ustalanie dat początkowych i końcowych
    if not transactions:
        logging.warning("⚠️ Brak transakcji w pliku PDF.")
        start = end = today
    else:
        start = transactions[0][0]
        end = transactions[-1][0]

    # Salda początkowe i końcowe
    cd60 = 'D' if saldo_pocz.startswith('-') else 'C'
    cd62 = 'D' if saldo_konc.startswith('-') else 'C'
    amt60 = pad_amount(saldo_pocz.lstrip('-'))
    amt62 = pad_amount(saldo_konc.lstrip('-'))

    lines = [
        f":20:{num_20}",
        f":25:{acct}",
        f":28C:{num_28C}",
        f":60F:{cd60}{start}PLN{amt60}"
    ]

    # Generowanie transakcji
    for idx, (d, a, desc) in enumerate(transactions):
        try:
            txn_type = 'D' if a.startswith('-') else 'C'
            
            # W MT940 Field 61: RRMMDD (data waluty) [RRMM] (data księgowania)
            # Używamy d (data waluty) i d[2:6] (miesiąc i dzień) jako daty księgowania
            entry_date = d[2:6] if len(d) >= 6 else d # Domyślnie MM DD
            
            amt = pad_amount(a.lstrip('-'))
            code = map_transaction_code(desc)
            num_code = code[1:] if code.startswith('N') else code # Kod numeryczny dla Field 86

            # Generowanie Field 61. Używamy //NONREF jako referencji klienta, jeśli nie wyodrębniono innej.
            if code == 'NTRFNONREF': # Jeśli jakimś cudem kod jest już pełny (wątpliwe)
                 lines.append(f":61:{d}{entry_date}{txn_type}{amt}{code}")
                 code_for_86 = 'TRF' 
            else:
                 lines.append(f":61:{d}{entry_date}{txn_type}{amt}{code}//NONREF")
                 code_for_86 = num_code
            
            # Segmentacja i dodanie Field 86
            segments = segment_description(desc, code_for_86)
            for seg in segments:
                # MT940 dopuszcza 65 znaków na linię :86:. Dzielimy dłuższe segmenty.
                # Używamy //00/ dla ciągłego opisu
                current_line = f":86:"
                remaining_text = seg
                
                # Jeśli segment ma prefiks (np. /20, /38), umieszczamy go tylko w pierwszej linii.
                prefix = re.match(r'^/\d{2,4}', remaining_text)
                if prefix:
                    remaining_text = remaining_text[prefix.end():]
                    current_line += prefix.group(0)

                # Dzielenie pozostałego tekstu
                while remaining_text:
                    if len(current_line) < 65:
                        can_fit = 65 - len(current_line)
                        lines.append(current_line + remaining_text[:can_fit])
                        remaining_text = remaining_text[can_fit:]
                        current_line = ":86:"
                    else:
                        # Jeśli pozostały tekst jest za długi na 65 znaków (co się nie powinno zdarzyć, ale zabezpieczenie)
                        # Dodajemy nową linię :86: z kontynuacją
                        lines.append(current_line)
                        current_line = ":86:"
                    
                    # W kolejnych wierszach dodajemy separator
                    if current_line == ":86:" and remaining_text:
                        current_line += "//00/" 


        except Exception as e:
            logging.exception("Błąd w transakcji #%d (Data: %s, Kwota: %s)", idx+1, d, a)
            lines.append(f":61:{d}{d[2:]}C00000000,00NTRF//ERROR")
            lines.append(":86:/00❌ BLAD PARSOWANIA OPISU TRANSAKCJI")

    lines.append(f":62F:{cd62}{end}PLN{amt62}")
    lines.append(f":64:{cd62}{end}PLN{amt62}")
    lines.append("-")

    mt940 = "\n".join(lines)
    return remove_trailing_86(mt940)

def save_mt940_file(mt940_text, output_path):
    """Zapisuje plik, używając kodowania Windows-1250 i CRLF."""
    try:
        with open(output_path, "w", encoding="windows-1250", newline="\r\n") as f:
            f.write(mt940_text)
    except Exception as e:
        logging.error(f"Błąd zapisu w Windows-1250: {e}. Zapisuję w UTF-8.")
        with open(output_path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(mt940_text)


def main():
    parser = argparse.ArgumentParser(description="Konwerter PDF do MT940")
    # Pamiętaj, że w Twoim środowisku Render ścieżki są przekazywane prawdopodobnie
    # z Node.js, więc argumenty wejścia i wyjścia muszą być obsługiwane.
    parser.add_argument("input_pdf", help="Ścieżka do pliku wejściowego PDF.")
    parser.add_argument("output_mt940", help="Ścieżka do pliku wyjściowego MT940.")
    parser.add_argument("--debug", action="store_true", help="Włącz tryb debugowania (wypis tekstu PDF oraz testowe MT940).")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    text = parse_pdf_text(args.input_pdf)

    if args.debug:
        print("\n=== WYPIS EKSTRAKTU Z PDF (DEBUG) ===")
        # Pamiętaj, aby skopiować ten tekst i przetestować na regex101.com jeśli problem nadal występuje!
        print(text)
        print("============================\n")

    account, sp, sk, tx, num_20, num_28C = pekao_parser(text)
    
    # Dodatkowe logowanie ile transakcji znaleziono
    print(f"\nLICZBA TRANSAKCJI : {len(tx)}\n")
    
    mt940 = build_mt940(account, sp, sk, tx, num_20, num_28C)

    if args.debug:
        print("\n=== Pierwsze 10 linii MT940 (DEBUG) ===")
        print("\n".join(mt940.splitlines()[:10]))
        print("============================\n")

    save_mt940_file(mt940, args.output_mt940)
    print("✅ Konwersja zakończona! Plik zapisany jako %s (kodowanie WINDOWS-1250)." % args.output_mt940)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.exception(e)
        sys.exit(1)