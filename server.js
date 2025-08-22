const express = require('express');
const multer = require('multer');
const path = require('path');
const fs = require('fs');
const cors = require('cors');

const app = express();

// 🔓 Obsługa CORS
app.use(cors({
  origin: 'http://finconvert.cba.pl', // lub '*' dla testów
  methods: ['POST'],
}));

// 📁 Upewnij się, że foldery istnieją
const ensureDir = (dir) => {
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
};
ensureDir('uploads');
ensureDir('outputs');

// 📤 Konfiguracja uploadu
const upload = multer({ dest: 'uploads/' });

app.post('/api/upload', upload.single('pdf'), (req, res) => {
  try {
    if (!req.file) {
      return res.status(400).json({ success: false, message: 'Brak pliku PDF.' });
    }

    const pdfPath = req.file.path;

    // 🔄 Tu podłącz parser PDF → MT940
    const { spawn } = require('child_process');

	const outputPath = path.join(__dirname, 'outputs', `${req.file.filename}.mt940`);
	const python = spawn('python', ['converter_web.py', pdfPath, outputPath]);

	python.on('close', (code) => {
	  if (code === 0) {
		res.json({
		  success: true,
		  downloadUrl: `/downloads/${req.file.filename}.mt940`
		});
	  } else {
		res.status(500).json({ success: false, message: 'Błąd konwersji.' });
	  }
});

  } catch (err) {
    console.error('❌ Błąd podczas przetwarzania:', err);
    res.status(500).json({ success: false, message: 'Błąd serwera.' });
  }
});

// 📥 Udostępnianie plików do pobrania
app.use('/downloads', express.static(path.join(__dirname, 'outputs')));

// 🚀 Start serwera
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`✅ Serwer działa na http://localhost:${PORT}`);
});