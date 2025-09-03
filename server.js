// server.js
const express = require('express');
const multer = require('multer');
const path = require('path');
const { spawn } = require('child_process');
const fs = require('fs');
const cors = require('cors');

const app = express();
const port = 3000;

app.use(cors());

// foldery
const uploadFolder = path.join(__dirname, 'uploads');
const outputFolder = path.join(__dirname, 'outputs');

if (!fs.existsSync(uploadFolder)) fs.mkdirSync(uploadFolder);
if (!fs.existsSync(outputFolder)) fs.mkdirSync(outputFolder);

// polskie nazwy miesięcy z dużej litery
const monthNamesPL = [
  'Styczeń','Luty','Marzec','Kwiecień','Maj','Czerwiec',
  'Lipiec','Sierpień','Wrzesień','Październik','Listopad','Grudzień'
];

// sanitizacja nazw plików
function sanitizeFilename(name) {
  return name
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/\s+/g, '_')
    .replace(/[^a-zA-Z0-9_\-\.]/g, '');
}

// formatowanie nazwy pliku wynikowego
function formatOutputFilename(originalName) {
  const baseName = path.basename(originalName, path.extname(originalName));
  const sanitizedName = sanitizeFilename(baseName);
  const now = new Date();
  const timestamp = now.toISOString().slice(0, 19).replace(/[:T]/g, '-');
  return `${sanitizedName}_${timestamp}.mt940`;
}

// multer
const storage = multer.diskStorage({
  destination: (req, file, cb) => cb(null, uploadFolder),
  filename:    (req, file, cb) => cb(null, sanitizeFilename(file.originalname))
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

// endpoint konwersji
app.post('/convert', upload.single('file'), (req, res) => {
  if (!req.file) {
    return res.status(400).json({ success: false, message: 'Nie przesłano pliku PDF.' });
  }

  const scriptPath   = path.join(__dirname, 'converter_web.py');
  const pdfPath      = path.join(uploadFolder, req.file.filename);
  const outputFilename = formatOutputFilename(req.file.filename);
  const outputPath   = path.join(outputFolder, outputFilename);

  const python = spawn('python', [ scriptPath, pdfPath, outputPath ]);

  let stdoutData = '';
  let stderrData = '';

  // timeout 60s
  const timeout = setTimeout(() => {
    python.kill('SIGKILL');
    return res.status(500).json({
      success: false,
      message: 'Przekroczono limit czasu konwersji (60s).'
    });
  }, 60000);

  python.stdout.on('data', data => {
    stdoutData += data.toString();
    console.log(`✅ Output: ${data.toString()}`);
  });

  python.stderr.on('data', data => {
    stderrData += data.toString();
    console.error(`❌ Błąd Pythona: ${data.toString()}`);
  });

  python.on('close', code => {
    clearTimeout(timeout);

    // wykrycie miesiąca
    let statementMonth = 'Nieznany';
    const monthPatterns = [
      /📅\s*Miesiąc wyciągu:\s*([^\n\r]+)/,   // pierwotny
      /(\d{2})\.(\d{2})\.(\d{4})/,            // dd.mm.yyyy
      /(\d{4})-(\d{2})-(\d{2})/,              // yyyy-mm-dd
      /(\d{2})\/(\d{4})/                      // MM/YYYY
    ];

    for (const rx of monthPatterns) {
      const m = stdoutData.match(rx);
      if (m) {
        if (rx === monthPatterns[0]) {
          statementMonth = m[1].trim();
        } else if (rx === monthPatterns[1]) {
          const mm = parseInt(m[2], 10);
          statementMonth = `${monthNamesPL[mm-1]} ${m[3]}`;
        } else if (rx === monthPatterns[2]) {
          const mm = parseInt(m[2], 10);
          statementMonth = `${monthNamesPL[mm-1]} ${m[1]}`;
        } else if (rx === monthPatterns[3]) {
          const mm = parseInt(m[1], 10);
          statementMonth = `${monthNamesPL[mm-1]} ${m[2]}`;
        }
        break;
      }
    }

    // wykrycie banku
    let statementBank = 'Nieznany';
    const bankMatch = stdoutData.match(/Wykryty bank:\s*([^\n\r]+)/);
    if (bankMatch && bankMatch[1]) {
      const raw = bankMatch[1].trim();
      statementBank = raw.charAt(0).toUpperCase() +
                      raw.slice(1).toLowerCase();
    }

    console.log(`🕓 Miesiąc: ${statementMonth}, 🏦 Bank: ${statementBank}`);

    // log konwersji
    fs.appendFileSync('conversion.log',
      `${new Date().toISOString()} - ${req.file.filename} → ${outputFilename}` +
      ` (miesiąc: ${statementMonth}, bank: ${statementBank})\n`
    );

    if (code === 0) {
      return res.json({
        success: true,
        message: 'Konwersja zakończona sukcesem.',
        output: stdoutData,
        downloadUrl: `https://finconvert-backend-1.onrender.com/outputs/${outputFilename}`,
        statementMonth,
        statementBank
      });
    } else {
      return res.status(500).json({
        success: false,
        message: 'Błąd konwersji.',
        error: stderrData
      });
    }
  });
});

// statyczne pliki
app.use(express.static(path.join(__dirname, 'public')));
app.use('/outputs', express.static(outputFolder));

app.listen(port, () => {
  console.log(`✅ Serwer działa na http://localhost:${port}`);
});
