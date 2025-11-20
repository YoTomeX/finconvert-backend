// server.js
const express    = require('express');
const multer     = require('multer');
const path       = require('path');
const { spawn }  = require('child_process');
const fs         = require('fs');
const cors       = require('cors');

const app  = express();
const port = const port = parseInt(process.env.PORT || "3000", 10);

// Konfiguracja CORS - tylko jedna, solidna linia middleware!
app.use(cors({
  origin: 'http://finconvert.cba.pl', // możesz tutaj istotnie podać dokładną domenę frontendu
  methods: ['GET', 'POST', 'OPTIONS'],
  allowedHeaders: ['Content-Type', 'Authorization']
}));

const uploadFolder = path.join(__dirname, 'uploads');
const outputFolder = path.join(__dirname, 'outputs');
if (!fs.existsSync(uploadFolder)) fs.mkdirSync(uploadFolder);
if (!fs.existsSync(outputFolder)) fs.mkdirSync(outputFolder);

const monthNamesPL = [
  'Styczeń','Luty','Marzec','Kwiecień','Maj','Czerwiec',
  'Lipiec','Sierpień','Wrzesień','Październik','Listopad','Grudzień'
];

function sanitizeFilename(name) {
  return name
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/\s+/g, '_')
    .replace(/[^a-zA-Z0-9_\-\.]/g, '');
}

function formatOutputFilename(originalName) {
  const baseName  = path.basename(originalName, path.extname(originalName));
  const sanitized = sanitizeFilename(baseName);
  const timestamp = new Date().toISOString().slice(0,19).replace(/[:T]/g,'-');
  return `${sanitized}_${timestamp}.mt940`;
}

const storage = multer.diskStorage({
  destination: (req,file,cb) => cb(null, uploadFolder),
  filename:    (req,file,cb) => cb(null, sanitizeFilename(file.originalname))
});
const upload = multer({
  storage,
  fileFilter: (req,file,cb) => {
    if (file.mimetype !== 'application/pdf') {
      return cb(new Error('Tylko pliki PDF są obsługiwane.'));
    }
    cb(null, true);
  }
});

app.post('/convert', upload.single('file'), (req, res) => {
  if (!req.file) {
    return res.status(400).json({ success:false, message:'Nie przesłano pliku PDF.' });
  }

  const scriptPath     = path.join(__dirname, 'converter_web.py');
  const pdfPath        = path.join(uploadFolder, req.file.filename);
  const outputFilename = formatOutputFilename(req.file.filename);
  const outputPath     = path.join(outputFolder, outputFilename);

  const python = spawn('python', [ scriptPath, pdfPath, outputPath ]);
  let stdoutData = '';
  let stderrData = '';

  const timeout = setTimeout(() => {
    python.kill('SIGKILL');
    return res.status(500).json({
      success: false,
      message: 'Przekroczono limit czasu konwersji (60s).'
    });
  }, 60000);

  python.stdout.on('data', data => {
    stdoutData += data.toString();
    console.log(`✅ Output: ${data}`);
  });

  python.stderr.on('data', data => {
    stderrData += data.toString();
    console.error(`❌ Błąd Pythona: ${data}`);
  });

  python.on('close', code => {
    clearTimeout(timeout);

    let numberOfTransactions = 0;
    try {
      const mt940Contents = fs.readFileSync(outputPath, 'utf-8');
      numberOfTransactions = (mt940Contents.match(/^[ \t]*:61:/gm) || []).length;
      console.log(`LICZBA TRANSAKCJI : ${numberOfTransactions}`);
    } catch (e) {
      console.error('Nie mogę odczytać pliku lub nie znalazłem fraz :61:.', e);
      numberOfTransactions = 0;
    }

    let statementMonth = 'Nieznany';
    const monthPatterns = [
      /📅\s*Miesiąc wyciągu:\s*([^\n\r]+)/,
      /Miesiąc:\s*([^\n\r]+)/,
      /(\d{2})\.(\d{2})\.(\d{4})/,
      /(\d{4})-(\d{2})-(\d{2})/,
      /(\d{2})\/(\d{4})/,
      /Za okres od \d{2}\/(\d{2})\/(\d{4})/
    ];
    for (const rx of monthPatterns) {
      const m = stdoutData.match(rx);
      if (m) {
        if (rx === monthPatterns[0] || rx === monthPatterns[1]) {
          statementMonth = m[1].trim();
        } else if (rx === monthPatterns[2]) {
          statementMonth = `${monthNamesPL[parseInt(m[2],10)-1]} ${m[3]}`;
        } else if (rx === monthPatterns[3]) {
          statementMonth = `${monthNamesPL[parseInt(m[2],10)-1]} ${m[1]}`;
        } else if (rx === monthPatterns[4]) {
          statementMonth = `${monthNamesPL[parseInt(m[1],10)-1]} ${m[2]}`;
        } else if (rx === monthPatterns[5]) {
          statementMonth = `${monthNamesPL[parseInt(m[1],10)-1]} ${m[2]}`;
        }
        break;
      }
    }
    if (statementMonth === 'Nieznany') {
      const base = path.basename(req.file.filename, path.extname(req.file.filename));
      const d    = base.match(/^(\d{4})(\d{2})/);
      if (d) {
        const [ , yy, mmRaw ] = d;
        const mm = parseInt(mmRaw,10);
        if (mm >=1 && mm <=12) {
          statementMonth = `${monthNamesPL[mm-1]} ${yy}`;
        }
      }
    }

    let statementBank = 'Nieznany';
    if (/PKOPPLPW|Pekao|Bank Polska Kasa Opieki/i.test(stdoutData)) {
      statementBank = 'Pekao';
    } else if (/Santander/i.test(stdoutData)) {
      statementBank = 'Santander';
    } else if (/mBank/i.test(stdoutData)) {
      statementBank = 'mBank';
    }
    if (statementBank === 'Nieznany') {
      const tokens = sanitizeFilename(req.file.filename).split('_');
      if (tokens.length >= 2) {
        const cand = tokens[1];
        statementBank = cand.charAt(0).toUpperCase() + cand.slice(1).toLowerCase();
      }
    }
    if (!statementMonth || statementMonth.length < 3) statementMonth = 'Nieznany';
    if (!statementBank || statementBank.length < 2) statementBank = 'Nieznany';

    console.log(`🕓 Miesiąc: ${statementMonth}, 🏦 Bank: ${statementBank}, 💸 Liczba transakcji: ${numberOfTransactions}`);

    fs.appendFileSync('conversion.log',
      `${new Date().toISOString()} - ${req.file.filename} → ${outputFilename}` +
      ` (miesiąc: ${statementMonth}, bank: ${statementBank}, liczba transakcji: ${numberOfTransactions})\n`
    );

    // Loguj odpowiedź JSON przed zwróceniem
    console.log('ODPOWIEDŹ JSON:', {
      success:       true,
      message:       'Konwersja zakończona sukcesem.',
      output:        stdoutData,
      downloadUrl:   `https://finconvert-backend-1.onrender.com/outputs/${outputFilename}`,
      statementMonth,
      statementBank,
      numberOfTransactions
    });

    if (code === 0) {
      return res.json({
        success:       true,
        message:       'Konwersja zakończona sukcesem.',
        output:        stdoutData,
        downloadUrl:   `https://finconvert-backend-1.onrender.com/outputs/${outputFilename}`,
        statementMonth,
        statementBank,
        numberOfTransactions
      });
    } else {
      return res.status(500).json({
        success: false,
        message: 'Błąd konwersji.',
        error:   stderrData
      });
    }
  });
});

app.use(express.static(path.join(__dirname, 'public')));
app.use((req, res, next) => {
  console.log(`${req.method} ${req.url}`);
  next();
});

app.use('/outputs', express.static(outputFolder));

app.listen(port, () => {
  console.log(`✅ Serwer działa na http://localhost:${port}`);
});
