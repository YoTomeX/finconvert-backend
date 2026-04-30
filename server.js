// server.js
const express    = require('express');
const multer     = require('multer');
const path       = require('path');
const { spawn }  = require('child_process');
const fs         = require('fs');
const cors       = require('cors');

const app  = express();
const port = parseInt(process.env.PORT || "3000", 10);
const pythonBin = process.env.PYTHON_BIN || 'python3';
const maxUploadMb = parseInt(process.env.MAX_UPLOAD_MB || "15", 10);
const outputBaseUrl = process.env.OUTPUT_BASE_URL || 'https://finconvert-backend-1.onrender.com';
const retentionHours = parseInt(process.env.FILE_RETENTION_HOURS || "72", 10);

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
  limits: { fileSize: maxUploadMb * 1024 * 1024 },
  fileFilter: (req,file,cb) => {
    if (file.mimetype !== 'application/pdf') {
      return cb(new Error('Tylko pliki PDF są obsługiwane.'));
    }
    cb(null, true);
  }
});

function isPdfMagic(filePath) {
  try {
    const fd = fs.openSync(filePath, 'r');
    const buf = Buffer.alloc(5);
    fs.readSync(fd, buf, 0, 5, 0);
    fs.closeSync(fd);
    return buf.toString('utf8') === '%PDF-';
  } catch (_) {
    return false;
  }
}

function cleanupOldFiles(dirPath, maxAgeMs) {
  if (!fs.existsSync(dirPath)) return;
  const now = Date.now();
  for (const name of fs.readdirSync(dirPath)) {
    const full = path.join(dirPath, name);
    try {
      const st = fs.statSync(full);
      if (!st.isFile()) continue;
      if (now - st.mtimeMs > maxAgeMs) fs.unlinkSync(full);
    } catch (e) {
      console.warn(`⚠️ Nie mogę usunąć ${full}:`, e.message);
    }
  }
}

app.post('/convert', upload.single('file'), (req, res) => {
  if (!req.file) {
    return res.status(400).json({ success:false, message:'Nie przesłano pliku PDF.' });
  }
  cleanupOldFiles(uploadFolder, retentionHours * 3600 * 1000);
  cleanupOldFiles(outputFolder, retentionHours * 3600 * 1000);

  const scriptPath     = path.join(__dirname, 'converter_web.py');
  const pdfPath        = path.join(uploadFolder, req.file.filename);
  const outputFilename = formatOutputFilename(req.file.filename);
  const outputPath     = path.join(outputFolder, outputFilename);

  if (!isPdfMagic(pdfPath)) {
    try { fs.unlinkSync(pdfPath); } catch (_) {}
    return res.status(400).json({
      success: false,
      code: 'INVALID_PDF_SIGNATURE',
      message: 'Plik nie jest poprawnym PDF (brak sygnatury %PDF-).'
    });
  }

  const python = spawn(pythonBin, [ scriptPath, pdfPath, outputPath ]);
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

	// Poluj TYLKO na: "Miesiąc wyciągu: XYZ"
	const monthRegex = /Miesiąc wyciągu:\s*([^\n\r]+)/;
	const m = stdoutData.match(monthRegex);
	if (m) {
		statementMonth = m[1].trim();
	} else {
		// Fallback: z nazwy pliku
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
      downloadUrl:   `${outputBaseUrl}/outputs/${outputFilename}`,
      statementMonth,
      statementBank,
      numberOfTransactions
    });

    if (code === 0) {
      return res.json({
        success:       true,
        message:       'Konwersja zakończona sukcesem.',
        output:        stdoutData,
        downloadUrl:   `${outputBaseUrl}/outputs/${outputFilename}`,
        statementMonth,
        statementBank,
        numberOfTransactions
      });
    } else {
      if (code === 3) {
        return res.status(422).json({
          success: false,
          code: 'UNSUPPORTED_BANK_PARSER',
          message: 'Wykryto bank, dla którego parser nie jest jeszcze obsługiwany.',
          error: stderrData
        });
      }
      return res.status(500).json({
        success: false,
        code: 'CONVERSION_ERROR',
        message: 'Błąd konwersji.',
        error: stderrData
      });
    }
  });
});

app.use(express.static(path.join(__dirname, 'public')));
app.use((req, res, next) => {
  console.log(`${req.method} ${req.url}`);
  next();
});

app.use((err, req, res, next) => {
  if (!err) return next();
  if (err.code === 'LIMIT_FILE_SIZE') {
    return res.status(413).json({
      success: false,
      code: 'FILE_TOO_LARGE',
      message: `Plik jest zbyt duży. Maksymalny rozmiar to ${maxUploadMb} MB.`
    });
  }
  if (err.message === 'Tylko pliki PDF są obsługiwane.') {
    return res.status(400).json({
      success: false,
      code: 'INVALID_FILE_TYPE',
      message: err.message
    });
  }
  return res.status(500).json({
    success: false,
    code: 'SERVER_ERROR',
    message: 'Nieoczekiwany błąd serwera.'
  });
});

app.use('/outputs', express.static(outputFolder));

app.listen(port, () => {
  console.log(`✅ Serwer działa na http://localhost:${port}`);
});
