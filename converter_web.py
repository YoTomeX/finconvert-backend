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
    cleaned = re.sub(r'[^A-Z0-9\s,\.\-/\(\)\?\:\+\r\n]', ' ', no_comb.upper())
    
    # 4. Usunięcie nadmiarowych spacji
    return re.sub(r'\s+',' ',cleaned).strip()

def clean_amount(amount):
    """Czysci kwotę do formatu numerycznego (przecinek jako separator dziesiętny)."""
    s = str(amount).replace('\xa0','').replace(' ','').replace('.', '').replace(',', '.')
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
    m28c = re.search(r'(Numer wyciągu|Nr wyciągu|Wyciąg nr)\s*[:\-]?\s*(\d{4})[\/\-]?\d{4}', text, re.I)
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
            segments.append(f"^{prefix}{clean_value}")
            seen.add(key)

    # 1. Segment KOD TRANSAKCJI (np. ^NTRF)
    # Zapewniamy, że kod z Field 61 jest w segmencie 86
    segments.append(f"^{code}")

    # 2. IBAN Odbiorcy/Nadawcy (^38)
    ibans = re.findall(r'(PL\d{26})', desc)
    for iban in ibans:
        add_segment("38", iban)

    # 3. Numer Referencyjny / Numer Dokumentu (^20)
    ref = re.search(r'NR REF[ .:]*([A-Z0-9]+)', desc)
    if ref:
        add_segment("20", ref.group(1))
    
    # 4. Numer Faktury (często w formacie F/2024/01)
    invoice = re.search(r'(FAKTURA|F\/|NR FAKTURY|T:).{0,50}', desc, re.I)
    if invoice:
        # Próbujemy znaleźć pełną frazę po kluczowym słowie
        line = invoice.group(0).strip()
        # Usuwamy ewentualne początkowe T: lub FAKTURA:
        clean_line = re.sub(r'^(FAKTURA|T|NR FAKTURY|F):?\s*', '', line, re.I).strip()
        if clean_line:
            add_segment("20", clean_line)

    # 5. Nazwa Kontrahenta (^32)
    # W Pekao nazwa jest często w osobnym wierszu. Szukamy ciągu dużych liter
    # Ten regex jest bardzo ogólny i może wymagać dostrojenia do konkretnych PDF-ów
    name = re.search(r'[A-Z][A-Z\s\.\,\-\']{5,100}', desc)
    if name:
        val = name.group(0).strip()
        # Wykluczenie ogólnych słów i numerów kont (jeśli nie zostały wyłapane przez IBAN)
        if not any(kw in val for kw in ['FAKTURA', 'PRZELEW', 'ZUS', 'KASA', 'BANK']) and len(val.split()) > 1:
            add_segment("32", val)
            
    # 6. Główny opis, jeśli nie ma ^00 (Cała reszta)
    if not any(s.startswith("^00") for s in segments):
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
    """
    account = ""; saldo_pocz = "0,00"; saldo_konc = "0,00"
    transactions = []
    
    # 1. Parsowanie nagłówków
    num_20, num_28C = extract_mt940_headers(text)
    
    # 2. Parsowanie konta i sald
    lines = text.splitlines()
    for line in lines:
        acc = re.search(r'(PL\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4})', line)
        if acc: account = re.sub(r'\s+', '', acc.group(1))
        sp = re.search(r'SALDO POCZĄTKOWE\s*[:\-]?\s*([\-\s\d,]+)', line, re.I)
        if sp: saldo_pocz = clean_amount(sp.group(1))
        sk = re.search(r'SALDO KOŃCOWE\s*[:\-]?\s*([\-\s\d,]+)', line, re.I)
        if sk: saldo_konc = clean_amount(sk.group(1))
    
    # 3. Parsowanie transakcji
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        
        # Precyzyjne dopasowanie transakcji (Data, Opcjonalna Kwota, Opis)
        # Szukamy linii zaczynającej się od DD/MM/RRRR, po której może być Kwota
        # (.*?) - Opis transakcji (może być pusty, ale niech zbiera cokolwiek)
        # ([\-\d\s,\.]+) - Kwota z opcjonalnym minusem (na końcu wiersza)
        
        # WZORZEC A: Data + Opis + Kwota na końcu
        m_a = re.match(r'^(\d{2}\/\d{2}\/\d{4})\s+(.*?)\s+([\-\d\s,]+)\s*$', line)
        
        # WZORZEC B: Tylko Data (np. na początku bloku, jeśli kwota jest w następnym wierszu lub kolumnie)
        m_b = re.match(r'^(\d{2}\/\d{2}\/\d{4})', line)

        dt = None
        amt = None
        
        if m_a:
            dt_raw = m_a.group(1)
            desc_part = m_a.group(2)
            amt_raw = m_a.group(3)
            
            # Wzrost niezawodności: Sprawdź, czy opis nie jest tylko datą księgowania
            if re.match(r'\d{2}\/\d{2}\/\d{4}', desc_part.strip()):
                m_a = None # Odrzucamy to dopasowanie, jeśli to format DW | DK | Opis | Kwota (i DW/DK się zlały)
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
                
        # Jeśli WZORZEC A nie zadziałał, próbujemy WZORZEC B i liczymy na zebranie opisu w kolejnych wierszach
        if m_b:
            dt_raw = m_b.group(1)
            
            # Zaczynamy zbierać opis od wiersza z datą
            desc_lines = [line.lstrip(dt_raw).strip()]
            
            dt = datetime.strptime(dt_raw, "%d/%m/%Y").strftime("%y%m%d")
            
            # Przechodzenie do następnej linii w poszukiwaniu opisu/kwoty
            j = i + 1
            kwota_znaleziona = False
            
            while j < len(lines) and not re.match(r'^\d{2}\/\d{2}\/\d{4}', lines[j].strip()) and lines[j].strip():
                next_line = lines[j].strip()
                
                # Szukamy kwoty w bieżącej linii kontynuacji
                amt_match = re.search(r'([\-\d\s,]+)\s*PLN\s*$', next_line)
                if amt_match and not kwota_znaleziona:
                    amt = clean_amount(amt_match.group(1))
                    kwota_znaleziona = True
                    # Usuwamy kwotę z opisu
                    next_line = re.sub(r'([\-\d\s,]+)\s*PLN\s*$', '', next_line).strip()
                
                if next_line:
                    desc_lines.append(next_line)
                
                j += 1
                
            # Wstawiamy transakcję tylko jeśli znaleźliśmy datę i kwotę
            if dt and amt:
                desc = " ".join(desc_lines).strip()
                transactions.append((dt, amt, desc))
                i = j
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
            # W Pekao Data Waluty i Data Księgowania są bliskie. Używamy daty waluty (d)
            # i dodajemy miesiąc i dzień (d[2:6]) jako datę księgowania (lub odwrotnie)
            entry_date = d[2:6] if len(d) >= 6 else d # Domyślnie MM DD
            
            amt = pad_amount(a.lstrip('-'))
            code = map_transaction_code(desc)
            num_code = code[1:] if code.startswith('N') else code # Kod numeryczny dla Field 86

            # Generowanie Field 61. Używamy NTRF jako domyślnej referencji, jeśli map_transaction_code zwraca NTRF.
            # Jeśli map_transaction_code zwraca kod (np. N240), wstawiamy go jako kod banku.
            # W Field 61 po kodzie powinna być referencja (np. //NONREF)
            
            # W MT940: :61:RRMMDD[RRMM]C/D[KWOTA]N[KOD_BANKU]//[REF_KLIENTA]
            
            # Sprawdzenie, czy kod jest już pełny (np. 'NTRFNONREF')
            if code == 'NTRFNONREF':
                 lines.append(f":61:{d}{entry_date}{txn_type}{amt}{code}")
                 code_for_86 = 'TRF' # Używamy TRF dla 86, jeśli kod jest 'NTRF'
            else:
                 # Zakładamy, że referencja klienta nie została znaleziona w tym kroku, używamy //NONREF
                 lines.append(f":61:{d}{entry_date}{txn_type}{amt}{code}//NONREF")
                 code_for_86 = num_code
            
            # Segmentacja i dodanie Field 86
            segments = segment_description(desc, code_for_86)
            for seg in segments:
                lines.append(f":86:{seg}")

        except Exception as e:
            logging.exception("Błąd w transakcji #%d (Data: %s, Kwota: %s)", idx+1, d, a)
            lines.append(f":61:{d}{d[2:]}C00000000,00NTRF//ERROR")
            lines.append(":86:^00❌ BLAD PARSOWANIA OPISU TRANSAKCJI")

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
    parser.add_argument("input_pdf", help="Ścieżka do pliku wejściowego PDF.")
    parser.add_argument("output_mt940", help="Ścieżka do pliku wyjściowego MT940.")
    parser.add_argument("--debug", action="store_true", help="Włącz tryb debugowania (wypis tekstu PDF oraz testowe MT940).")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    text = parse_pdf_text(args.input_pdf)

    if args.debug:
        print("\n=== WYPIS EKSTRAKTU Z PDF ===")
        print(text)
        print("============================\n")

    account, sp, sk, tx, num_20, num_28C = pekao_parser(text)
    mt940 = build_mt940(account, sp, sk, tx, num_20, num_28C)

    if args.debug:
        print("\n=== Pierwsze 10 linii MT940 ===")
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