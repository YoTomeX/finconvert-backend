# 📄 FinConvert Backend

FinConvert to aplikacja webowa umożliwiająca konwersję plików PDF z wyciągami bankowymi na format MT940, zgodny z systemami księgowymi. Backend został zbudowany w Node.js i wykorzystuje skrypt Pythona do analizy danych bankowych.

FinConvert is a web application that converts bank statement PDFs into MT940 format, compatible with accounting systems. The backend is built with Node.js and uses a Python script for parsing bank data.

---

## 🚀 Demo

- Backend: https://finconvert-backend-1.onrender.com  
- Frontend: http://finconvert.cba.pl

---

## ⚙️ Technologie / Technologies

- Node.js + Express  
- Multer (upload plików / file upload)  
- Python + pdfplumber (parser PDF → MT940)  
- CORS (komunikacja z frontendem / frontend communication)  
- Render (hosting backendu / backend hosting)

---

## 📦 Instalacja lokalna / Local Installation

```bash
git clone https://github.com/YoTomeX/finconvert-backend.git
cd finconvert-backend
npm install
