import sys
import os
import locale
from datetime import datetime
import re
import pdfplumber
import traceback
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

IBAN_REGEX = re.compile(r'(PL\s?\d{2}\s?(\d{4}\s?){6}\d{4}|PL\d{24}|\d{26})', re.IGNORECASE)

def parse_pdf_text(pdf_path):
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = []
            for page in pdf.pages:
                extracted = page.extract_text()
                if extracted:
                    text.append(extracted)
            return "\n".join(text)
    except Exception as e:
        raise ValueError(f"Nie można odczytać PDF: {e}")

def clean_amount(amount):
    if not amount:
        return "0,00"
    amount = amount.replace('\xa0', '').replace(' ', '').replace('.', '')
    amount = amount.replace(',', '.').strip()
    try:
        return "{:.2f}".format(float(amount)).replace('.', ',')
    except:
        return "0,00"

def format_description(raw_desc):
    desc = raw_desc.replace('\n', ' ').strip()
    desc = re.sub(r'\s+', ' ', desc)
    return desc[:200] if len(desc) > 200 else desc

def build_mt940(account, saldo_pocz, saldo_konc, transactions):
    today = datetime.now().strftime("%y%m%d")
    start_date = transactions[0]['date'] if transactions else today
    end_date = transactions[-1]['date'] if transactions else today

    mt940 = [
        ":20:STMT1",
        f":25:{account}",
        ":28C:0001",
        f":60F:C{start_date}PLN{saldo_pocz}"
    ]

    for txn in transactions:
        date = txn['date']
        amount = txn['amount'].replace(',', '.')  # Konwertujemy z powrotem na kropkę dla MT940
        type_code = 'C' if float(amount) > 0 else 'D'
        clean_amt = abs(float(amount)).__format__(",.2f").replace(',', '.').replace('.', ',')
        
        mt940.append(f":61:{date}{date}{type_code}{clean_amt}NTRF")
        mt940.append(f":86:/TXT/{format_description(txn['description'])}")

    mt940.append(f":62F:C{end_date}PLN{saldo_konc}")
    mt940.append(f":64:C{end_date}PLN{saldo_konc}")
    return "\n".join(mt940)

def save_file(mt940, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="windows-1250") as f:
        f.write(mt940)

def extract_bank_account(text):
    account = re.search(IBAN_REGEX, text)
    return account.group().replace(' ', '') if account else "00000000000000000000000000"

def detect_bank(text):
    text_lower = text.lower()
    if "santander" in text_lower:
        return "santander"
    elif "pekao" in text_lower:
        return "pekao"
    elif "mbank" in text_lower:
        return "mbank"
    return None

def santander_parser(text):
    transactions = []
    try:
        saldo_pocz = re.search(r"saldo początkowe.*?(\d+[.,]\d+)", text, re.DOTALL).group(1)
        saldo_konc = re.search(r"saldo końcowe.*?(\d+[.,]\d+)", text, re.DOTALL).group(1)
        
        entries = re.findall(r'(\d{2}/\d{2}/\d{4}.*?)(?=\d{2}/\d{2}/\d{4}|\Z)', text, re.DOTALL)
        
        for entry in entries:
            date_str = re.search(r'(\d{2}/\d{2}/\d{4})', entry).group(1)
            date = datetime.strptime(date_str, "%d/%m/%Y").strftime("%y%m%d")
            amount = re.search(r'([-+]?\d+[.,]\d+)', entry).group(1)
            desc = re.sub(r'\d+[.,]\d+', '', entry).strip()
            
            transactions.append({
                'date': date,
                'amount': clean_amount(amount),
                'description': desc
            })
    except Exception as e:
        print(f"Błąd Santander parser: {str(e)}")
        
    return {
        "account": extract_bank_account(text),
        "saldo_pocz": clean_amount(saldo_pocz),
        "saldo_konc": clean_amount(saldo_konc),
        "transactions": transactions
    }

def pekao_parser(text):
    transactions = []
    try:
        saldo_pocz = re.search(r"SALDO POCZ.*?(\d+[.,]\d+)", text, re.DOTALL).group(1)
        saldo_konc = re.search(r"SALDO KON.*?(\d+[.,]\d+)", text, re.DOTALL).group(1)
        
        entries = re.findall(r'(\d{2}\.\d{2}\.\d{4}.*?)(?=\d{2}\.\d{2}\.\d{4}|\Z)', text, re.DOTALL)
        
        for entry in entries:
            date_str = re.search(r'(\d{2}\.\d{2}\.\d{4})', entry).group(1)
            date = datetime.strptime(date_str, "%d.%m.%Y").strftime("%y%m%d")
            amount = re.search(r'([-+]?\d+[.,]\d+)', entry).group(1)
            desc = re.sub(r'\d+[.,]\d+', '', entry).strip()
            
            transactions.append({
                'date': date,
                'amount': clean_amount(amount),
                'description': desc
            })
    except Exception as e:
        print(f"Błąd Pekao parser: {str(e)}")
        
    return {
        "account": extract_bank_account(text),
        "saldo_pocz": clean_amount(saldo_pocz),
        "saldo_konc": clean_amount(saldo_konc),
        "transactions": transactions
    }

BANK_PARSERS = {
    "santander": santander_parser,
    "pekao": pekao_parser
}

def convert(pdf_path, output_path):
    try:
        text = parse_pdf_text(pdf_path)
        bank = detect_bank(text)
        
        if not bank or bank not in BANK_PARSERS:
            raise ValueError("Nieznany bank lub parser niedostępny")
        
        parser = BANK_PARSERS[bank]
        data = parser(text)
        
        if not data["transactions"]:
            raise ValueError("Brak transakcji w pliku")
        
        mt940 = build_mt940(
            data["account"],
            data["saldo_pocz"],
            data["saldo_konc"],
            data["transactions"]
        )
        
        save_file(mt940, output_path)
        print(f"Utworzono plik: {output_path}")
        
    except Exception as e:
        print(f"Błąd: {str(e)}")
        traceback.print_exc()

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Użycie: python converter.py input.pdf output.mt940")
        sys.exit(1)
    
    convert(sys.argv[1], sys.argv[2])