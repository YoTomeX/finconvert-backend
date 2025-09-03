const express = require('express');
const multer = require('multer');
const path = require('path');
const { spawn } = require('child_process');
const fs = require('fs');
const cors = require('cors');

const app = express();
const port = 3000;

app.use(cors());

// Foldery
const uploadFolder = path.join(__dirname, 'uploads');
const outputFolder = path.join(__dirname, 'outputs');

if (!fs.existsSync(uploadFolder)) fs.mkdirSync(uploadFolder);
if (!fs.existsSync(outputFolder)) fs.mkdirSync(outputFolder);

// Funkcja do sanitizacji nazw plików
function sanitizeFilename(name) {
  return name
    .normalize('NFD') // rozdziela znaki diakrytyczne
    .replace(/[\u0300-\u036f]/g, '') // usuwa diakrytyki
    .replace(/\s+/g, '_') // zamienia spacje na _
    .replace(/[^a-zA-Z0-9_\-\.]/g, ''); // usuwa niedozwolone znaki
}

// Funkcja do generowania nazwy pliku wynikowego
function formatOutputFilename(originalName) {
  const baseName = path.basename(originalName, path.extname(originalName));
  const sanitizedName = sanitizeFilename(baseName);
  const now = new Date();
  const timestamp = now.toISOString().slice(0, 19).replace(/[:T]/g, '-');
  return `${sanitizedName}_${timestamp}.mt940`;
}

// Konfiguracja Multer
const storage = multer.diskStorage({
  destination: (req, file, cb) => cb(null, uploadFolder),
  filename: (req, file, cb) => cb(null, sanitizeFilename(file.originalname))
});
const upload = multer({ 
  storage,
  fileFilter: (req, file, cb) => {
    if (file.mimetype !== 'application/pdf') {
      return cb(new Error('Tylko pliki PDF są obsługiwane.'));
    }
    cb(null, true);
  }
});

// Endpoint konwersji
app.post('/convert', upload.single('file'), (req, res) => {
  if (!req.file) {
    return res.status(400).json({ success: false, message: 'Nie przesłano pliku PDF.' });
  }

  const scriptPath = path.join(__dirname, 'converter_web.py');
  const pdfPath = path.join(uploadFolder, req.file.filename);
  const outputFilename = formatOutputFilename(req.file.filename);
  const outputPath = path.join(outputFolder, outputFilename);

  const python = spawn('python', [scriptPath, pdfPath, outputPath]);

  let stdoutData = '';
  let stderrData = '';

  const timeout = setTimeout(() => {
    python.kill();
    return res.status(500).json({ success: false, message: 'Przekroczono limit czasu konwersji.' });
  }, 15000); // 15 sekund

  python.stdout.on('data', (data) => {
    stdoutData += data.toString();
    console.log(`✅ Output: ${data.toString()}`);
  });

  python.stderr.on('data', (data) => {
    stderrData += data.toString();
    console.error(`❌ Błąd Pythona: ${data.toString()}`);
  });

  python.on('close', (code) => {
    clearTimeout(timeout);

    const monthMatch = stdoutData.match(/📅 Miesiąc wyciągu: ([^\n\r]+)/);
    const statementMonth = monthMatch ? monthMatch[1].trim() : 'Nieznany';

    // Logowanie konwersji
    fs.appendFileSync('conversion.log', `${new Date().toISOString()} - ${req.file.filename} → ${outputFilename}\n`);

    if (code === 0) {
      res.json({
        success: true,
        message: 'Konwersja zakończona sukcesem.',
        output: stdoutData,
        downloadUrl: `https://finconvert-backend-1.onrender.com/outputs/${outputFilename}`,
        statementMonth: statementMonth
      });
    } else {
      res.status(500).json({
        success: false,
        message: 'Błąd konwersji.',
        error: stderrData
      });
    }
  });
});

// Serwowanie frontendu i plików wynikowych
app.use(express.static(path.join(__dirname, 'public')));
app.use('/outputs', express.static(outputFolder));

app.listen(port, () => {
  console.log(`✅ Serwer działa na http://localhost:${port}`);
});
